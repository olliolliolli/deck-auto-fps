/*
 * AutoFPS FSR2→FSR4 bridge для RE Engine (RE4 Remake) — етап 3: РОБОЧИЙ МІСТ.
 *
 * Гра працює з рідним FSR 2 (3D у низькій роздільності, інтерфейс — у повній). Міст стежить за
 * командами D3D12 і на кроці RCAS (останньому кроці FSR 2, ДО постобробки й інтерфейсу) замість
 * нього викликає FidelityFX API (FSR 4 / 3.1) з тими самими вхідними даними гри:
 *   колір (рендер-роздільність), глибина, вектори руху, зсув камери; вихід — та сама текстура,
 *   яку далі бере постобробка. Тому інтерфейс гра малює вже поверх результату FSR 4 — чітким.
 * Якщо щось не так (немає FFX, бракує ресурсу) — гра працює з рідним FSR 2, нічого не ламається.
 *
 * Налаштування: autofps_fsr.ini поруч з re4.exe (key=value), журнал: autofps_fsr.log.
 * Ліцензія: BSD-3-Clause. MinHook — BSD-2-Clause, DirectX-Headers — MIT, FidelityFX API — MIT.
 */
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <d3d12.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <limits.h>
#include <string.h>
#include "MinHook.h"
#include "ffx_api.h"
#include "ffx_upscale.h"
#include "dx12/ffx_api_dx12.h"
#include <math.h>

/* ------------------------------------------------------------------ журнал */

static FILE *g_log;
static CRITICAL_SECTION g_cs;
static DWORD g_t0;

static void logf_(const char *fmt, ...)
{
    if (!g_log) return;
    EnterCriticalSection(&g_cs);
    fprintf(g_log, "[%8.3f] ", (GetTickCount() - g_t0) / 1000.0);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
    LeaveCriticalSection(&g_cs);
}
#define LOG(...) logf_(__VA_ARGS__)

static void open_log(void)
{
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, path, MAX_PATH);
    while (n > 0 && path[n - 1] != L'\\' && path[n - 1] != L'/') n--;
    path[n] = 0;
    wcscat(path, L"autofps_fsr.log");
    g_log = _wfopen(path, L"w");
}

/* вікна запису: через 60 с, далі щохвилини, 6 разів; у кожному — перші кадри з FSR 2 */
static DWORD g_win_start_ms = 60000, g_win_period_ms = 60000, g_win_count = 6;
static int cur_window(void)
{
    DWORD t = GetTickCount() - g_t0;
    if (t < g_win_start_ms) return -1;
    DWORD k = (t - g_win_start_ms) / g_win_period_ms;
    if (k >= g_win_count || (t - g_win_start_ms) % g_win_period_ms > 3000) return -1;
    return (int)k;
}

/* ------------------------------------------------------------------ кроки FSR 2 у RE4 (етап 1) */

typedef struct { uint64_t hash; const char *name; } known_pso;
static const known_pso g_known[] = {
    {0x77501c64360b3290ULL, "fsr2-1 prep(160x90)"},
    {0xfacf8f57b36f482fULL, "fsr2-2 VelocityTranslate"},
    {0x373b42588c9e5f23ULL, "fsr2-3 (160x90)"},
    {0x6c5793de6ada1e41ULL, "fsr2-4 LumaPyramid(SPD)"},
    {0xcf46b20eab6d171bULL, "fsr2-5 (160x90)"},
    {0x50b5ea4724b30cb8ULL, "fsr2-6 (160x90)"},
    {0x921fd0029d14a74bULL, "fsr2-7 (160x90)"},
    {0x1dd0c5db17408375ULL, "fsr2-8 (160x90)"},
    {0xc669a9db25ea4759ULL, "fsr2-9 ACCUMULATE(display)"},
    {0xc7e4d3dc11696abfULL, "fsr2-10 RCAS(display)"},
    {0x7cd9e63795fb23f6ULL, "post-1 (після FSR)"},
};
static const char *known_name(uint64_t h)
{
    for (size_t i = 0; i < sizeof(g_known) / sizeof(g_known[0]); i++)
        if (g_known[i].hash == h) return g_known[i].name;
    return NULL;
}

/* ------------------------------------------------------------------ хеш-таблиці */

static uint32_t mix(uint64_t x, uint32_t cap)
{
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33;
    return (uint32_t)x & (cap - 1);
}

static uint64_t fnv1a(const void *p, size_t n)
{
    const uint8_t *b = (const uint8_t *)p;
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ULL; }
    return h;
}

/* PSO → хеш шейдера */
typedef struct { void *key; uint64_t hash; } pso_ent;
#define PSO_CAP (1u << 18)
static pso_ent *g_pso;
static SRWLOCK g_pso_lock = SRWLOCK_INIT;

static void pso_put(void *k, uint64_t h)
{
    AcquireSRWLockExclusive(&g_pso_lock);
    uint32_t i = mix((uint64_t)(uintptr_t)k, PSO_CAP);
    for (uint32_t n = 0; n < PSO_CAP; n++, i = (i + 1) & (PSO_CAP - 1))
        if (!g_pso[i].key || g_pso[i].key == k) { g_pso[i].key = k; g_pso[i].hash = h; break; }
    ReleaseSRWLockExclusive(&g_pso_lock);
}

static uint64_t pso_hash(void *k)
{
    uint64_t h = 0;
    AcquireSRWLockShared(&g_pso_lock);
    uint32_t i = mix((uint64_t)(uintptr_t)k, PSO_CAP);
    for (uint32_t n = 0; n < 64; n++, i = (i + 1) & (PSO_CAP - 1)) {
        if (g_pso[i].key == k) { h = g_pso[i].hash; break; }
        if (!g_pso[i].key) break;
    }
    ReleaseSRWLockShared(&g_pso_lock);
    return h;
}

/* дескриптор (CPU-адреса) → що в ньому */
enum { D_NONE = 0, D_SRV = 1, D_UAV = 2, D_CBV = 3 };
typedef struct {
    size_t key;
    void *res;
    uint64_t va;
    uint32_t size;
    uint16_t kind, fmt;
    uint16_t dim, pad;
} desc_ent;
#define DESC_CAP (1u << 21)
static desc_ent *g_desc;
static SRWLOCK g_desc_lock = SRWLOCK_INIT;

static void desc_put_locked(size_t k, const desc_ent *v)
{
    uint32_t i = mix(k, DESC_CAP);
    for (uint32_t n = 0; n < 4096; n++, i = (i + 1) & (DESC_CAP - 1)) {
        if (g_desc[i].key == 0 || g_desc[i].key == k) {
            g_desc[i] = *v;
            g_desc[i].key = k;
            return;
        }
    }
}

static int desc_get_locked(size_t k, desc_ent *out)
{
    uint32_t i = mix(k, DESC_CAP);
    for (uint32_t n = 0; n < 4096; n++, i = (i + 1) & (DESC_CAP - 1)) {
        if (g_desc[i].key == k) { *out = g_desc[i]; return 1; }
        if (g_desc[i].key == 0) return 0;
    }
    return 0;
}

/* купи дескрипторів */
typedef struct { size_t cpu; uint64_t gpu; uint32_t num, type, inc, visible; } heap_info;
static heap_info g_heaps[1024];
static volatile LONG g_nheaps;
static UINT g_inc[4];

static int gpu_to_cpu(uint64_t gpu, size_t *cpu, uint32_t *inc)
{
    LONG n = g_nheaps;
    for (LONG i = 0; i < n && i < 1024; i++) {
        heap_info *h = &g_heaps[i];
        if (h->visible && gpu >= h->gpu && gpu < h->gpu + (uint64_t)h->num * h->inc) {
            *cpu = h->cpu + (size_t)(gpu - h->gpu);
            *inc = h->inc;
            return 1;
        }
    }
    return 0;
}

/* кореневі сигнатури */
typedef struct { uint8_t type; uint32_t num, reg, space, offset; } range_info;
typedef struct { uint8_t type; uint8_t nranges; range_info r[8]; uint32_t reg, num32; } param_info;
typedef struct { void *rs; uint32_t nparams; param_info p[16]; } rs_info;
#define RS_CAP 8192
static rs_info *g_rs;
static volatile LONG g_nrs;

static rs_info *rs_find(void *rs)
{
    LONG n = g_nrs;
    for (LONG i = n - 1; i >= 0; i--)
        if (g_rs[i].rs == rs) return &g_rs[i];
    return NULL;
}

/* відображені буфери (для читання константних буферів) */
typedef struct { void *res; uint8_t *cpu; uint64_t va; uint64_t size; } map_info;
#define MAP_CAP 16384
static map_info *g_maps;
static volatile LONG g_nmaps;
static SRWLOCK g_map_lock = SRWLOCK_INIT;

static const uint8_t *va_to_cpu(uint64_t va, uint64_t need)
{
    const uint8_t *p = NULL;
    AcquireSRWLockShared(&g_map_lock);
    LONG n = g_nmaps;
    for (LONG i = 0; i < n; i++) {
        map_info *m = &g_maps[i];
        if (m->cpu && va >= m->va && va + need <= m->va + m->size) { p = m->cpu + (va - m->va); break; }
    }
    ReleaseSRWLockShared(&g_map_lock);
    return p;
}

/* стан командного списку */
typedef struct { void *cl; void *rs; void *pso; uint64_t tables[16]; ID3D12DescriptorHeap *heaps[2]; UINT nheaps; void *grs; uint64_t gtables[16]; } cl_state;
static __thread int t_in_ffx;   /* під час виклику FFX не записуємо стан (його ставить FFX) */
#define CL_CAP 4096
static cl_state g_cls[CL_CAP];
static SRWLOCK g_cl_lock = SRWLOCK_INIT;

static cl_state *cl_get(void *cl)
{
    uint32_t i = mix((uint64_t)(uintptr_t)cl, CL_CAP);
    for (uint32_t n = 0; n < CL_CAP; n++, i = (i + 1) & (CL_CAP - 1)) {
        if (g_cls[i].cl == cl) return &g_cls[i];
        if (!g_cls[i].cl) { g_cls[i].cl = cl; return &g_cls[i]; }
    }
    return &g_cls[0];
}

/* ------------------------------------------------------------------ хуки */

typedef HRESULT (STDMETHODCALLTYPE *PFN_CreateComputePSO)(ID3D12Device *, const D3D12_COMPUTE_PIPELINE_STATE_DESC *, REFIID, void **);
typedef HRESULT (STDMETHODCALLTYPE *PFN_CreatePSO2)(ID3D12Device2 *, const D3D12_PIPELINE_STATE_STREAM_DESC *, REFIID, void **);
typedef HRESULT (STDMETHODCALLTYPE *PFN_LoadComputePipeline)(ID3D12PipelineLibrary *, LPCWSTR, const D3D12_COMPUTE_PIPELINE_STATE_DESC *, REFIID, void **);
typedef HRESULT (STDMETHODCALLTYPE *PFN_CreateRootSignature)(ID3D12Device *, UINT, const void *, SIZE_T, REFIID, void **);
typedef HRESULT (STDMETHODCALLTYPE *PFN_CreateDescriptorHeap)(ID3D12Device *, const D3D12_DESCRIPTOR_HEAP_DESC *, REFIID, void **);
typedef void (STDMETHODCALLTYPE *PFN_CreateSRV)(ID3D12Device *, ID3D12Resource *, const D3D12_SHADER_RESOURCE_VIEW_DESC *, D3D12_CPU_DESCRIPTOR_HANDLE);
typedef void (STDMETHODCALLTYPE *PFN_CreateUAV)(ID3D12Device *, ID3D12Resource *, ID3D12Resource *, const D3D12_UNORDERED_ACCESS_VIEW_DESC *, D3D12_CPU_DESCRIPTOR_HANDLE);
typedef void (STDMETHODCALLTYPE *PFN_CreateCBV)(ID3D12Device *, const D3D12_CONSTANT_BUFFER_VIEW_DESC *, D3D12_CPU_DESCRIPTOR_HANDLE);
typedef void (STDMETHODCALLTYPE *PFN_CopyDescriptorsSimple)(ID3D12Device *, UINT, D3D12_CPU_DESCRIPTOR_HANDLE, D3D12_CPU_DESCRIPTOR_HANDLE, D3D12_DESCRIPTOR_HEAP_TYPE);
typedef void (STDMETHODCALLTYPE *PFN_CopyDescriptors)(ID3D12Device *, UINT, const D3D12_CPU_DESCRIPTOR_HANDLE *, const UINT *, UINT, const D3D12_CPU_DESCRIPTOR_HANDLE *, const UINT *, D3D12_DESCRIPTOR_HEAP_TYPE);
typedef HRESULT (STDMETHODCALLTYPE *PFN_Map)(ID3D12Resource *, UINT, const D3D12_RANGE *, void **);
typedef void (STDMETHODCALLTYPE *PFN_SetPipelineState)(ID3D12GraphicsCommandList *, ID3D12PipelineState *);
typedef void (STDMETHODCALLTYPE *PFN_Dispatch)(ID3D12GraphicsCommandList *, UINT, UINT, UINT);
typedef void (STDMETHODCALLTYPE *PFN_SetComputeRootSignature)(ID3D12GraphicsCommandList *, ID3D12RootSignature *);
typedef void (STDMETHODCALLTYPE *PFN_SetComputeRootDescriptorTable)(ID3D12GraphicsCommandList *, UINT, D3D12_GPU_DESCRIPTOR_HANDLE);
typedef void (STDMETHODCALLTYPE *PFN_SetComputeRoot32BitConstants)(ID3D12GraphicsCommandList *, UINT, UINT, const void *, UINT);
typedef void (STDMETHODCALLTYPE *PFN_SetComputeRootCBV)(ID3D12GraphicsCommandList *, UINT, D3D12_GPU_VIRTUAL_ADDRESS);
typedef HRESULT (WINAPI *PFN_CreateRSDeserializer)(LPCVOID, SIZE_T, REFIID, void **);
typedef void (STDMETHODCALLTYPE *PFN_SetDescriptorHeaps)(ID3D12GraphicsCommandList *, UINT, ID3D12DescriptorHeap *const *);
typedef void (STDMETHODCALLTYPE *PFN_SetGraphicsRootSignature)(ID3D12GraphicsCommandList *, ID3D12RootSignature *);
typedef void (STDMETHODCALLTYPE *PFN_SetGraphicsRootDescriptorTable)(ID3D12GraphicsCommandList *, UINT, D3D12_GPU_DESCRIPTOR_HANDLE);
typedef void (STDMETHODCALLTYPE *PFN_Reset)(ID3D12GraphicsCommandList *, ID3D12CommandAllocator *, ID3D12PipelineState *);

static PFN_CreateComputePSO o_CreateComputePSO;
static PFN_CreatePSO2 o_CreatePSO2;
static PFN_LoadComputePipeline o_LoadComputePipeline;
static PFN_CreateRootSignature o_CreateRootSignature;
static PFN_CreateDescriptorHeap o_CreateDescriptorHeap;
static PFN_CreateSRV o_CreateSRV;
static PFN_CreateUAV o_CreateUAV;
static PFN_CreateCBV o_CreateCBV;
static PFN_CopyDescriptorsSimple o_CopyDescriptorsSimple;
static PFN_CopyDescriptors o_CopyDescriptors;
static PFN_Map o_Map;
static PFN_SetPipelineState o_SetPipelineState;
static PFN_Dispatch o_Dispatch;
static PFN_SetComputeRootSignature o_SetComputeRootSignature;
static PFN_SetComputeRootDescriptorTable o_SetComputeRootDescriptorTable;
static PFN_SetComputeRoot32BitConstants o_SetComputeRoot32BitConstants;
static PFN_SetComputeRootCBV o_SetComputeRootCBV;
static PFN_CreateRSDeserializer p_CreateRSDeserializer;
static PFN_SetDescriptorHeaps o_SetDescriptorHeaps;
static PFN_Reset o_Reset;
static PFN_SetGraphicsRootSignature o_SetGraphicsRootSignature;
static PFN_SetGraphicsRootDescriptorTable o_SetGraphicsRootDescriptorTable;

static void register_pso(void *pso, const D3D12_SHADER_BYTECODE *cs)
{
    if (!pso || !cs || !cs->pShaderBytecode || !cs->BytecodeLength) return;
    uint64_t h = fnv1a(cs->pShaderBytecode, cs->BytecodeLength);
    pso_put(pso, h);
    const char *nm = known_name(h);
    if (nm) LOG("PSO %p = %s (hash %016llx)", pso, nm, (unsigned long long)h);
}

static HRESULT STDMETHODCALLTYPE h_CreateComputePSO(ID3D12Device *dev, const D3D12_COMPUTE_PIPELINE_STATE_DESC *d, REFIID riid, void **out)
{
    HRESULT hr = o_CreateComputePSO(dev, d, riid, out);
    if (SUCCEEDED(hr) && out && *out && d) register_pso(*out, &d->CS);
    return hr;
}

static HRESULT STDMETHODCALLTYPE h_LoadComputePipeline(ID3D12PipelineLibrary *lib, LPCWSTR name, const D3D12_COMPUTE_PIPELINE_STATE_DESC *d, REFIID riid, void **out)
{
    HRESULT hr = o_LoadComputePipeline(lib, name, d, riid, out);
    if (SUCCEEDED(hr) && out && *out && d) register_pso(*out, &d->CS);
    return hr;
}

static HRESULT STDMETHODCALLTYPE h_CreatePSO2(ID3D12Device2 *dev, const D3D12_PIPELINE_STATE_STREAM_DESC *d, REFIID riid, void **out)
{
    HRESULT hr = o_CreatePSO2(dev, d, riid, out);
    if (FAILED(hr) || !out || !*out || !d || !d->pPipelineStateSubobjectStream) return hr;
    /* compute PSO у потоці: ROOT_SIGNATURE (вказівник) + CS (D3D12_SHADER_BYTECODE), рідко інше */
    const uint8_t *p = (const uint8_t *)d->pPipelineStateSubobjectStream;
    size_t off = 0;
    while (off + 8 <= d->SizeInBytes) {
        int type = *(const int *)(p + off);
        if (type == D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_CS && off + 8 + sizeof(D3D12_SHADER_BYTECODE) <= d->SizeInBytes) {
            register_pso(*out, (const D3D12_SHADER_BYTECODE *)(p + off + 8));
            break;
        }
        if (type == D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_ROOT_SIGNATURE) off += 16;
        else if (type == D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_NODE_MASK || type == D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_FLAGS) off += 8;
        else if (type == D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_CACHED_PSO) off += 24;
        else break;
    }
    return hr;
}

static HRESULT STDMETHODCALLTYPE h_CreateRootSignature(ID3D12Device *dev, UINT mask, const void *blob, SIZE_T len, REFIID riid, void **out)
{
    HRESULT hr = o_CreateRootSignature(dev, mask, blob, len, riid, out);
    if (FAILED(hr) || !out || !*out || !p_CreateRSDeserializer) return hr;
    ID3D12RootSignatureDeserializer *des = NULL;
    if (FAILED(p_CreateRSDeserializer(blob, len, &IID_ID3D12RootSignatureDeserializer, (void **)&des)) || !des)
        return hr;
    const D3D12_ROOT_SIGNATURE_DESC *rd = ID3D12RootSignatureDeserializer_GetRootSignatureDesc(des);
    LONG idx = InterlockedIncrement(&g_nrs) - 1;
    if (rd && idx < RS_CAP) {
        rs_info *ri = &g_rs[idx];
        ri->nparams = rd->NumParameters < 16 ? rd->NumParameters : 16;
        for (UINT i = 0; i < ri->nparams; i++) {
            const D3D12_ROOT_PARAMETER *rp = &rd->pParameters[i];
            param_info *pi = &ri->p[i];
            pi->type = (uint8_t)rp->ParameterType;
            if (rp->ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE) {
                pi->nranges = (uint8_t)(rp->DescriptorTable.NumDescriptorRanges < 8 ? rp->DescriptorTable.NumDescriptorRanges : 8);
                for (UINT r = 0; r < pi->nranges; r++) {
                    const D3D12_DESCRIPTOR_RANGE *dr = &rp->DescriptorTable.pDescriptorRanges[r];
                    pi->r[r].type = (uint8_t)dr->RangeType;
                    pi->r[r].num = dr->NumDescriptors;
                    pi->r[r].reg = dr->BaseShaderRegister;
                    pi->r[r].space = dr->RegisterSpace;
                    pi->r[r].offset = dr->OffsetInDescriptorsFromTableStart;
                }
            } else if (rp->ParameterType == D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS) {
                pi->reg = rp->Constants.ShaderRegister;
                pi->num32 = rp->Constants.Num32BitValues;
            } else {
                pi->reg = rp->Descriptor.ShaderRegister;
            }
        }
        ri->rs = *out;   /* публікуємо останнім */
    }
    ID3D12RootSignatureDeserializer_Release(des);
    return hr;
}

static HRESULT STDMETHODCALLTYPE h_CreateDescriptorHeap(ID3D12Device *dev, const D3D12_DESCRIPTOR_HEAP_DESC *d, REFIID riid, void **out)
{
    HRESULT hr = o_CreateDescriptorHeap(dev, d, riid, out);
    if (SUCCEEDED(hr) && out && *out && d && d->Type <= D3D12_DESCRIPTOR_HEAP_TYPE_DSV) {
        ID3D12DescriptorHeap *h = (ID3D12DescriptorHeap *)*out;
        LONG i = InterlockedIncrement(&g_nheaps) - 1;
        if (i < 1024) {
            D3D12_CPU_DESCRIPTOR_HANDLE c; D3D12_GPU_DESCRIPTOR_HANDLE g = {0};
            ID3D12DescriptorHeap_GetCPUDescriptorHandleForHeapStart(h, &c);
            int vis = (d->Flags & D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE) != 0;
            if (vis) ID3D12DescriptorHeap_GetGPUDescriptorHandleForHeapStart(h, &g);
            g_heaps[i].num = d->NumDescriptors; g_heaps[i].type = d->Type; g_heaps[i].inc = g_inc[d->Type];
            g_heaps[i].cpu = c.ptr; g_heaps[i].gpu = g.ptr; g_heaps[i].visible = vis;
            LOG("HEAP type=%d num=%u visible=%d cpu=%llx gpu=%llx inc=%u", d->Type, d->NumDescriptors, vis,
                (unsigned long long)c.ptr, (unsigned long long)g.ptr, g_inc[d->Type]);
        }
    }
    return hr;
}

static void STDMETHODCALLTYPE h_CreateSRV(ID3D12Device *dev, ID3D12Resource *res, const D3D12_SHADER_RESOURCE_VIEW_DESC *d, D3D12_CPU_DESCRIPTOR_HANDLE h)
{
    o_CreateSRV(dev, res, d, h);
    desc_ent e = {0};
    e.kind = D_SRV; e.res = res;
    if (d) { e.fmt = (uint16_t)d->Format; e.dim = (uint16_t)d->ViewDimension; }
    AcquireSRWLockExclusive(&g_desc_lock); desc_put_locked(h.ptr, &e); ReleaseSRWLockExclusive(&g_desc_lock);
}

static void STDMETHODCALLTYPE h_CreateUAV(ID3D12Device *dev, ID3D12Resource *res, ID3D12Resource *ctr, const D3D12_UNORDERED_ACCESS_VIEW_DESC *d, D3D12_CPU_DESCRIPTOR_HANDLE h)
{
    o_CreateUAV(dev, res, ctr, d, h);
    desc_ent e = {0};
    e.kind = D_UAV; e.res = res;
    if (d) { e.fmt = (uint16_t)d->Format; e.dim = (uint16_t)d->ViewDimension; }
    AcquireSRWLockExclusive(&g_desc_lock); desc_put_locked(h.ptr, &e); ReleaseSRWLockExclusive(&g_desc_lock);
}

static void STDMETHODCALLTYPE h_CreateCBV(ID3D12Device *dev, const D3D12_CONSTANT_BUFFER_VIEW_DESC *d, D3D12_CPU_DESCRIPTOR_HANDLE h)
{
    o_CreateCBV(dev, d, h);
    desc_ent e = {0};
    e.kind = D_CBV;
    if (d) { e.va = d->BufferLocation; e.size = d->SizeInBytes; }
    AcquireSRWLockExclusive(&g_desc_lock); desc_put_locked(h.ptr, &e); ReleaseSRWLockExclusive(&g_desc_lock);
}

static void copy_range_locked(size_t dst, size_t src, UINT n, UINT inc)
{
    for (UINT i = 0; i < n; i++) {
        desc_ent e;
        if (desc_get_locked(src + (size_t)i * inc, &e)) desc_put_locked(dst + (size_t)i * inc, &e);
        else { desc_ent z = {0}; desc_put_locked(dst + (size_t)i * inc, &z); }
    }
}

static void STDMETHODCALLTYPE h_CopyDescriptorsSimple(ID3D12Device *dev, UINT n, D3D12_CPU_DESCRIPTOR_HANDLE dst, D3D12_CPU_DESCRIPTOR_HANDLE src, D3D12_DESCRIPTOR_HEAP_TYPE type)
{
    o_CopyDescriptorsSimple(dev, n, dst, src, type);
    if (type != D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV) return;
    AcquireSRWLockExclusive(&g_desc_lock);
    copy_range_locked(dst.ptr, src.ptr, n, g_inc[type]);
    ReleaseSRWLockExclusive(&g_desc_lock);
}

static void STDMETHODCALLTYPE h_CopyDescriptors(ID3D12Device *dev, UINT nd, const D3D12_CPU_DESCRIPTOR_HANDLE *ds, const UINT *dsz,
                                                UINT ns, const D3D12_CPU_DESCRIPTOR_HANDLE *ss, const UINT *ssz, D3D12_DESCRIPTOR_HEAP_TYPE type)
{
    o_CopyDescriptors(dev, nd, ds, dsz, ns, ss, ssz, type);
    if (type != D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV) return;
    UINT inc = g_inc[type];
    AcquireSRWLockExclusive(&g_desc_lock);
    UINT di = 0, doff = 0, si = 0, soff = 0;
    while (di < nd && si < ns) {
        UINT dn = dsz ? dsz[di] : 1, sn = ssz ? ssz[si] : 1;
        copy_range_locked(ds[di].ptr + (size_t)doff * inc, ss[si].ptr + (size_t)soff * inc, 1, inc);
        if (++doff >= dn) { di++; doff = 0; }
        if (++soff >= sn) { si++; soff = 0; }
    }
    ReleaseSRWLockExclusive(&g_desc_lock);
}

static HRESULT STDMETHODCALLTYPE h_Map(ID3D12Resource *res, UINT sub, const D3D12_RANGE *rr, void **pp)
{
    HRESULT hr = o_Map(res, sub, rr, pp);
    if (SUCCEEDED(hr) && pp && *pp && sub == 0) {
        D3D12_RESOURCE_DESC d;
        ID3D12Resource_GetDesc(res, &d);
        if (d.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER) {
            uint64_t va = ID3D12Resource_GetGPUVirtualAddress(res);
            AcquireSRWLockExclusive(&g_map_lock);
            LONG n = g_nmaps, i;
            for (i = 0; i < n; i++) if (g_maps[i].res == res) break;
            if (i == n && n < MAP_CAP) g_nmaps = n + 1;
            if (i < MAP_CAP) { g_maps[i].res = res; g_maps[i].cpu = (uint8_t *)*pp; g_maps[i].va = va; g_maps[i].size = d.Width; }
            ReleaseSRWLockExclusive(&g_map_lock);
        }
    }
    return hr;
}

static void STDMETHODCALLTYPE h_SetPipelineState(ID3D12GraphicsCommandList *cl, ID3D12PipelineState *pso)
{
    if (!t_in_ffx) { AcquireSRWLockExclusive(&g_cl_lock); cl_get(cl)->pso = pso; ReleaseSRWLockExclusive(&g_cl_lock); }
    o_SetPipelineState(cl, pso);
}

static void STDMETHODCALLTYPE h_SetComputeRootSignature(ID3D12GraphicsCommandList *cl, ID3D12RootSignature *rs)
{
    if (!t_in_ffx) { AcquireSRWLockExclusive(&g_cl_lock); cl_get(cl)->rs = rs; ReleaseSRWLockExclusive(&g_cl_lock); }
    o_SetComputeRootSignature(cl, rs);
}

static void STDMETHODCALLTYPE h_SetComputeRootDescriptorTable(ID3D12GraphicsCommandList *cl, UINT idx, D3D12_GPU_DESCRIPTOR_HANDLE h)
{
    if (idx < 16 && !t_in_ffx) { AcquireSRWLockExclusive(&g_cl_lock); cl_get(cl)->tables[idx] = h.ptr; ReleaseSRWLockExclusive(&g_cl_lock); }
    o_SetComputeRootDescriptorTable(cl, idx, h);
}

static void STDMETHODCALLTYPE h_SetComputeRoot32BitConstants(ID3D12GraphicsCommandList *cl, UINT idx, UINT n, const void *data, UINT off)
{
    o_SetComputeRoot32BitConstants(cl, idx, n, data, off);
}

static void STDMETHODCALLTYPE h_SetComputeRootCBV(ID3D12GraphicsCommandList *cl, UINT idx, D3D12_GPU_VIRTUAL_ADDRESS va)
{
    o_SetComputeRootCBV(cl, idx, va);
}

static const char *dim_name(D3D12_RESOURCE_DIMENSION d)
{
    switch (d) {
    case D3D12_RESOURCE_DIMENSION_BUFFER: return "buf";
    case D3D12_RESOURCE_DIMENSION_TEXTURE1D: return "tex1d";
    case D3D12_RESOURCE_DIMENSION_TEXTURE2D: return "tex2d";
    case D3D12_RESOURCE_DIMENSION_TEXTURE3D: return "tex3d";
    default: return "?";
    }
}

static void dump_cbv(uint64_t va, uint32_t size)
{
    uint32_t n = size ? size : 256;
    if (n > 512) n = 512;
    const uint8_t *p = va_to_cpu(va, n);
    if (!p) { LOG("        (вміст CBV недоступний: буфер не відображено)"); return; }
    const uint32_t *u = (const uint32_t *)p;
    for (uint32_t i = 0; i < n / 4; i += 8) {
        char line[512]; size_t k = 0;
        for (uint32_t j = i; j < i + 8 && j < n / 4; j++) {
            float f; memcpy(&f, &u[j], 4);
            k += (size_t)snprintf(line + k, sizeof(line) - k, "[%02u]%08x/%g ", j, u[j], f);
        }
        LOG("        %s", line);
    }
}

static void dump_dispatch(cl_state *st, const char *name, UINT x, UINT y, UINT z)
{
    LOG("=== %s  groups=%u,%u,%u  CL=%p RS=%p", name, x, y, z, st->cl, st->rs);
    rs_info *ri = rs_find(st->rs);
    if (!ri) { LOG("    (розкладка RS невідома)"); return; }
    for (uint32_t p = 0; p < ri->nparams; p++) {
        param_info *pi = &ri->p[p];
        if (pi->type == D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS) { LOG("  param %u: root constants b%u x%u", p, pi->reg, pi->num32); continue; }
        if (pi->type != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE) { LOG("  param %u: root descriptor type=%d reg=%u", p, pi->type, pi->reg); continue; }
        size_t cpu; uint32_t inc;
        if (!gpu_to_cpu(st->tables[p], &cpu, &inc)) { LOG("  param %u: table gpu=%llx (купа невідома)", p, (unsigned long long)st->tables[p]); continue; }
        LOG("  param %u: table gpu=%llx ranges=%u", p, (unsigned long long)st->tables[p], pi->nranges);
        uint32_t running = 0;
        for (uint32_t r = 0; r < pi->nranges; r++) {
            range_info *rg = &pi->r[r];
            static const char *rt[] = {"SRV", "UAV", "CBV", "SMP"};
            uint32_t start = rg->offset == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND ? running : rg->offset;
            uint32_t cnt = rg->num == UINT_MAX ? 4 : rg->num;
            if (cnt > 24) cnt = 24;
            LOG("    range %u: %s x%u base=%u space=%u off=%u", r, rg->type < 4 ? rt[rg->type] : "?", rg->num, rg->reg, rg->space, start);
            for (uint32_t j = 0; j < cnt; j++) {
                desc_ent e; int ok;
                AcquireSRWLockShared(&g_desc_lock);
                ok = desc_get_locked(cpu + (size_t)(start + j) * inc, &e);
                ReleaseSRWLockShared(&g_desc_lock);
                char regn = rg->type == 0 ? 't' : rg->type == 1 ? 'u' : 'b';
                if (!ok || e.kind == D_NONE) { LOG("      %c%u: (порожньо/невідомо)", regn, rg->reg + j); continue; }
                if (e.kind == D_CBV) {
                    LOG("      %c%u: CBV va=%llx size=%u", regn, rg->reg + j, (unsigned long long)e.va, e.size);
                    dump_cbv(e.va, e.size);
                    continue;
                }
                if (!e.res) { LOG("      %c%u: %s null-ресурс fmt=%u", regn, rg->reg + j, e.kind == D_SRV ? "SRV" : "UAV", e.fmt); continue; }
                D3D12_RESOURCE_DESC d;
                ID3D12Resource_GetDesc((ID3D12Resource *)e.res, &d);
                LOG("      %c%u: %s res=%p %s %llux%u d%u mips=%u resfmt=%d viewfmt=%u viewdim=%u flags=%x",
                    regn, rg->reg + j, e.kind == D_SRV ? "SRV" : "UAV", e.res, dim_name(d.Dimension),
                    (unsigned long long)d.Width, d.Height, d.DepthOrArraySize, d.MipLevels, d.Format, e.fmt, e.dim, d.Flags);
            }
            running = start + (rg->num == UINT_MAX ? 0 : rg->num);
        }
    }
}


/* ------------------------------------------------------------------ налаштування (autofps_fsr.ini) */

static struct {
    int enable;          /* 0 — лише рідний FSR 2 */
    uint32_t flags;      /* FfxApiCreateContextUpscaleFlags */
    int depth_conv;      /* 1 — глибина з кроку 3 (R32_FLOAT), 0 — оригінальна D32S8 */
    int reactive;        /* 1 — подавати маску з кроку 1 як reactive */
    int skip_accumulate; /* 1 — не виконувати крок накопичення FSR 2 (економія GPU) */
    float sharpness;     /* 0..1, 0 — без додаткової різкості */
    float cam_near, cam_far;
    char want[32];       /* «4» — FSR 4, «3» — FSR 3.1, «auto» — найновіший */
} g_cfg = {1, FFX_UPSCALE_ENABLE_HIGH_DYNAMIC_RANGE | FFX_UPSCALE_ENABLE_AUTO_EXPOSURE |
              FFX_UPSCALE_ENABLE_DEPTH_INVERTED | FFX_UPSCALE_ENABLE_DEPTH_INFINITE,
           0, 0, 0, 0.3f, 0.1f, 10000.0f, "4"};

static void load_cfg(void)
{
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, path, MAX_PATH);
    while (n > 0 && path[n - 1] != L'\\' && path[n - 1] != L'/') n--;
    path[n] = 0;
    wcscat(path, L"autofps_fsr.ini");
    FILE *f = _wfopen(path, L"r");
    if (!f) { LOG("autofps_fsr.ini немає — типові налаштування"); return; }
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        char k[64], v[128];
        if (line[0] == ';' || line[0] == '#' || sscanf(line, " %63[^= ] = %127s", k, v) != 2) continue;
        if (!strcmp(k, "enable")) g_cfg.enable = atoi(v);
        else if (!strcmp(k, "flags")) g_cfg.flags = (uint32_t)strtoul(v, NULL, 0);
        else if (!strcmp(k, "depth_conv")) g_cfg.depth_conv = atoi(v);
        else if (!strcmp(k, "reactive")) g_cfg.reactive = atoi(v);
        else if (!strcmp(k, "skip_accumulate")) g_cfg.skip_accumulate = atoi(v);
        else if (!strcmp(k, "sharpness")) g_cfg.sharpness = (float)atof(v);
        else if (!strcmp(k, "near")) g_cfg.cam_near = (float)atof(v);
        else if (!strcmp(k, "far")) g_cfg.cam_far = (float)atof(v);
        else if (!strcmp(k, "version")) { strncpy(g_cfg.want, v, sizeof(g_cfg.want) - 1); }
    }
    fclose(f);
    LOG("налаштування: enable=%d flags=0x%x depth_conv=%d reactive=%d skip_accumulate=%d sharpness=%.2f near=%g far=%g version=%s",
        g_cfg.enable, g_cfg.flags, g_cfg.depth_conv, g_cfg.reactive, g_cfg.skip_accumulate, g_cfg.sharpness,
        g_cfg.cam_near, g_cfg.cam_far, g_cfg.want);
}

/* ------------------------------------------------------------------ FidelityFX API */

static PfnFfxCreateContext f_create;
static PfnFfxDestroyContext f_destroy;
static PfnFfxQuery f_query;
static PfnFfxDispatch f_dispatch;
static PfnFfxConfigure f_configure;
static ffxContext g_ctx;
static uint32_t g_ctx_w, g_ctx_h;
static int g_ffx_state;            /* 0 — ще не пробували, 1 — готово, -1 — недоступно */
static char g_ffx_version[96];
static SRWLOCK g_ffx_lock = SRWLOCK_INIT;

static void ffx_msg(uint32_t type, const wchar_t *msg)
{
    char buf[512];
    WideCharToMultiByte(CP_UTF8, 0, msg, -1, buf, sizeof(buf), NULL, NULL);
    LOG("FFX %s: %s", type == FFX_API_MESSAGE_TYPE_ERROR ? "помилка" : "попередження", buf);
}

static int ffx_load(void)
{
    if (f_create) return 1;
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, path, MAX_PATH);
    while (n > 0 && path[n - 1] != L'\\' && path[n - 1] != L'/') n--;
    path[n] = 0;
    wcscat(path, L"amd_fidelityfx_dx12.dll");
    HMODULE m = LoadLibraryW(path);
    if (!m) { LOG("amd_fidelityfx_dx12.dll не знайдено поруч з грою — міст вимкнено"); return 0; }
    f_create = (PfnFfxCreateContext)GetProcAddress(m, "ffxCreateContext");
    f_destroy = (PfnFfxDestroyContext)GetProcAddress(m, "ffxDestroyContext");
    f_query = (PfnFfxQuery)GetProcAddress(m, "ffxQuery");
    f_dispatch = (PfnFfxDispatch)GetProcAddress(m, "ffxDispatch");
    f_configure = (PfnFfxConfigure)GetProcAddress(m, "ffxConfigure");
    if (!f_create || !f_dispatch || !f_query) { LOG("amd_fidelityfx_dx12.dll без потрібних функцій"); f_create = NULL; return 0; }
    return 1;
}

/* вибір версії апскейлера: FSR 4 (якщо є), інакше найновіша */
static uint64_t ffx_pick_version(ID3D12Device *dev)
{
    uint64_t count = 0;
    struct ffxQueryDescGetVersions q = {0};
    q.header.type = FFX_API_QUERY_DESC_TYPE_GET_VERSIONS;
    q.createDescType = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    q.device = dev;
    q.outputCount = &count;
    if (f_query(NULL, &q.header) != FFX_API_RETURN_OK || count == 0) { LOG("FFX: версії апскейлера не знайдено"); return 0; }
    if (count > 16) count = 16;
    uint64_t ids[16] = {0};
    const char *names[16] = {0};
    q.versionIds = ids; q.versionNames = names;
    f_query(NULL, &q.header);
    uint64_t pick = 0; int best = -1;
    for (uint64_t i = 0; i < count; i++) {
        const char *nm = names[i] ? names[i] : "?";
        LOG("FFX: доступна версія %llu: %s (id=%llx)", (unsigned long long)i, nm, (unsigned long long)ids[i]);
        int major = atoi(nm);
        int score = major * 100;   /* перша в списку — найновіша в межах «мажорної» */
        if (g_cfg.want[0] == '4' && major == 4) score += 1000;
        if (g_cfg.want[0] == '3' && major == 3) score += 1000;
        if (score > best) { best = score; pick = ids[i]; snprintf(g_ffx_version, sizeof(g_ffx_version), "%s", nm); }
    }
    LOG("FFX: вибрано %s", g_ffx_version);
    return pick;
}

static int ffx_ensure(ID3D12Device *dev, uint32_t w, uint32_t h)
{
    if (g_ffx_state < 0) return 0;
    if (g_ffx_state == 1 && g_ctx_w == w && g_ctx_h == h) return 1;
    if (!ffx_load()) { g_ffx_state = -1; return 0; }
    if (g_ctx) { f_destroy(&g_ctx, NULL); g_ctx = NULL; }
    uint64_t ver = ffx_pick_version(dev);

    struct ffxCreateContextDescUpscale cd = {0};
    struct ffxCreateBackendDX12Desc bd = {0};
    struct ffxOverrideVersion ov = {0};
    cd.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    cd.flags = g_cfg.flags;
    cd.maxRenderSize.width = w; cd.maxRenderSize.height = h;
    cd.maxUpscaleSize.width = w; cd.maxUpscaleSize.height = h;
    cd.fpMessage = ffx_msg;
    bd.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12;
    bd.device = dev;
    cd.header.pNext = &bd.header;
    if (ver) {
        ov.header.type = FFX_API_DESC_TYPE_OVERRIDE_VERSION;
        ov.versionId = ver;
        bd.header.pNext = &ov.header;
    }
    ffxReturnCode_t rc = f_create(&g_ctx, &cd.header, NULL);
    if (rc != FFX_API_RETURN_OK) {
        LOG("FFX: ffxCreateContext = %u — міст вимкнено, лишається рідний FSR 2", rc);
        g_ctx = NULL; g_ffx_state = -1; return 0;
    }
    struct ffxQueryGetProviderVersion pv = {0};
    pv.header.type = FFX_API_QUERY_DESC_TYPE_GET_PROVIDER_VERSION;
    if (f_query(&g_ctx, &pv.header) == FFX_API_RETURN_OK && pv.versionName)
        snprintf(g_ffx_version, sizeof(g_ffx_version), "%s", pv.versionName);
    LOG("FFX: контекст створено, %ux%u, апскейлер %s", w, h, g_ffx_version);
    g_ctx_w = w; g_ctx_h = h; g_ffx_state = 1;
    return 1;
}

/* ------------------------------------------------------------------ ресурси поточного кадру */

typedef struct {
    void *cl;
    ID3D12Resource *color, *depth, *depth_conv, *mv, *reactive;
    uint32_t cb[64];
    int have_cb;
    int accumulate_skipped;
} frame_state;
static frame_state g_fr;            /* FSR 2 рендериться одним командним списком за кадр */
static SRWLOCK g_fr_lock = SRWLOCK_INIT;

static ID3D12Resource *table_res(cl_state *st, uint32_t param, uint32_t reg_index, int want_kind)
{
    rs_info *ri = rs_find(st->rs);
    if (!ri || param >= ri->nparams) return NULL;
    param_info *pi = &ri->p[param];
    if (pi->type != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE || pi->nranges < 1) return NULL;
    size_t cpu; uint32_t inc;
    if (!gpu_to_cpu(st->tables[param], &cpu, &inc)) return NULL;
    uint32_t start = pi->r[0].offset == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND ? 0 : pi->r[0].offset;
    desc_ent e;
    int ok;
    AcquireSRWLockShared(&g_desc_lock);
    ok = desc_get_locked(cpu + (size_t)(start + reg_index) * inc, &e);
    ReleaseSRWLockShared(&g_desc_lock);
    if (!ok || e.kind != want_kind) return NULL;
    return (ID3D12Resource *)e.res;
}

/* перший ресурс потрібного формату в таблиці (рушій інколи переставляє слоти) */
static ID3D12Resource *table_find(cl_state *st, uint32_t param, DXGI_FORMAT fmt, uint32_t w, uint32_t h, int want_kind)
{
    for (uint32_t i = 0; i < 16; i++) {
        ID3D12Resource *r = table_res(st, param, i, want_kind);
        if (!r) continue;
        D3D12_RESOURCE_DESC d;
        ID3D12Resource_GetDesc(r, &d);
        if (d.Format == fmt && (!w || (d.Width == w && d.Height == h))) return r;
    }
    return NULL;
}

static int table_cbv(cl_state *st, uint32_t param, uint32_t reg_index, uint32_t *out, uint32_t n)
{
    rs_info *ri = rs_find(st->rs);
    if (!ri || param >= ri->nparams) return 0;
    size_t cpu; uint32_t inc;
    if (!gpu_to_cpu(st->tables[param], &cpu, &inc)) return 0;
    desc_ent e; int ok;
    AcquireSRWLockShared(&g_desc_lock);
    ok = desc_get_locked(cpu + (size_t)reg_index * inc, &e);
    ReleaseSRWLockShared(&g_desc_lock);
    if (!ok || e.kind != D_CBV) return 0;
    const uint8_t *p = va_to_cpu(e.va, n * 4);
    if (!p) return 0;
    memcpy(out, p, n * 4);
    return 1;
}

static struct FfxApiResource ffx_res(ID3D12Resource *r, uint32_t state)
{
    struct FfxApiResource x;
    memset(&x, 0, sizeof(x));
    x.resource = r;
    x.state = state;
    if (!r) return x;
    D3D12_RESOURCE_DESC d;
    ID3D12Resource_GetDesc(r, &d);
    x.description.type = FFX_API_RESOURCE_TYPE_TEXTURE2D;
    x.description.width = (uint32_t)d.Width;
    x.description.height = d.Height;
    x.description.depth = d.DepthOrArraySize;
    x.description.mipCount = d.MipLevels;
    switch (d.Format) {
    case DXGI_FORMAT_R11G11B10_FLOAT: x.description.format = FFX_API_SURFACE_FORMAT_R11G11B10_FLOAT; break;
    case DXGI_FORMAT_R16G16B16A16_FLOAT: x.description.format = FFX_API_SURFACE_FORMAT_R16G16B16A16_FLOAT; break;
    case DXGI_FORMAT_R16G16_FLOAT: x.description.format = FFX_API_SURFACE_FORMAT_R16G16_FLOAT; break;
    case DXGI_FORMAT_R32_FLOAT: case DXGI_FORMAT_D32_FLOAT: x.description.format = FFX_API_SURFACE_FORMAT_R32_FLOAT; break;
    case DXGI_FORMAT_R32G8X24_TYPELESS: case DXGI_FORMAT_D32_FLOAT_S8X24_UINT: case DXGI_FORMAT_R32_FLOAT_X8X24_TYPELESS:
        x.description.format = FFX_API_SURFACE_FORMAT_R32_FLOAT; break;
    case DXGI_FORMAT_R8_UNORM: x.description.format = FFX_API_SURFACE_FORMAT_R8_UNORM; break;
    case DXGI_FORMAT_R8G8B8A8_UNORM: x.description.format = FFX_API_SURFACE_FORMAT_R8G8B8A8_UNORM; break;
    case DXGI_FORMAT_R10G10B10A2_UNORM: x.description.format = FFX_API_SURFACE_FORMAT_R10G10B10A2_UNORM; break;
    default: x.description.format = FFX_API_SURFACE_FORMAT_UNKNOWN; break;
    }
    x.description.usage = FFX_API_RESOURCE_USAGE_READ_ONLY;
    if (d.Format == DXGI_FORMAT_R32G8X24_TYPELESS || d.Format == DXGI_FORMAT_D32_FLOAT_S8X24_UINT)
        x.description.usage = FFX_API_RESOURCE_USAGE_DEPTHTARGET | FFX_API_RESOURCE_USAGE_STENCILTARGET;
    else if (d.Format == DXGI_FORMAT_D32_FLOAT)
        x.description.usage = FFX_API_RESOURCE_USAGE_DEPTHTARGET;
    if (d.Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS) x.description.usage |= FFX_API_RESOURCE_USAGE_UAV;
    if (d.Flags & D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET) x.description.usage |= FFX_API_RESOURCE_USAGE_RENDERTARGET;
    return x;
}

/* статистика для журналу */
static volatile LONG g_bridged, g_fallback;
static DWORD g_last_stat;
static LARGE_INTEGER g_qpf, g_last_qpc;
static int g_logged_first;

static void restore_state(ID3D12GraphicsCommandList *cl, const cl_state *st)
{
    if (st->nheaps) o_SetDescriptorHeaps(cl, st->nheaps, st->heaps);
    if (st->rs) o_SetComputeRootSignature(cl, (ID3D12RootSignature *)st->rs);
    rs_info *ri = rs_find(st->rs);
    uint32_t np = ri ? ri->nparams : 16;
    for (uint32_t i = 0; i < np && i < 16; i++) {
        if (ri && ri->p[i].type != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE) continue;
        if (st->tables[i]) {
            D3D12_GPU_DESCRIPTOR_HANDLE h; h.ptr = st->tables[i];
            o_SetComputeRootDescriptorTable(cl, i, h);
        }
    }
    if (st->pso) o_SetPipelineState(cl, (ID3D12PipelineState *)st->pso);
    if (st->grs) {
        o_SetGraphicsRootSignature(cl, (ID3D12RootSignature *)st->grs);
        rs_info *gi = rs_find(st->grs);
        for (uint32_t i = 0; i < 16; i++) {
            if (gi && (i >= gi->nparams || gi->p[i].type != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE)) continue;
            if (st->gtables[i]) { D3D12_GPU_DESCRIPTOR_HANDLE h; h.ptr = st->gtables[i]; o_SetGraphicsRootDescriptorTable(cl, i, h); }
        }
    }
}

static void STDMETHODCALLTYPE h_SetGraphicsRootSignature(ID3D12GraphicsCommandList *cl, ID3D12RootSignature *rs)
{
    if (!t_in_ffx) { AcquireSRWLockExclusive(&g_cl_lock); cl_get(cl)->grs = rs; ReleaseSRWLockExclusive(&g_cl_lock); }
    o_SetGraphicsRootSignature(cl, rs);
}

static void STDMETHODCALLTYPE h_SetGraphicsRootDescriptorTable(ID3D12GraphicsCommandList *cl, UINT idx, D3D12_GPU_DESCRIPTOR_HANDLE h)
{
    if (idx < 16 && !t_in_ffx) { AcquireSRWLockExclusive(&g_cl_lock); cl_get(cl)->gtables[idx] = h.ptr; ReleaseSRWLockExclusive(&g_cl_lock); }
    o_SetGraphicsRootDescriptorTable(cl, idx, h);
}

/* повертає 1, якщо крок замінено (оригінальний Dispatch не виконувати) */
static int bridge_rcas(ID3D12GraphicsCommandList *cl, cl_state *st)
{
    frame_state fr;
    AcquireSRWLockShared(&g_fr_lock); fr = g_fr; ReleaseSRWLockShared(&g_fr_lock);
    ID3D12Resource *out = table_res(st, 2, 0, D_UAV);
    ID3D12Resource *depth = g_cfg.depth_conv ? fr.depth_conv : fr.depth;
    const char *why = NULL;
    if (fr.cl != cl) why = "інший командний список";
    else if (!fr.color) why = "немає кольору";
    else if (!depth) why = "немає глибини";
    else if (!fr.mv) why = "немає векторів руху";
    else if (!fr.have_cb) why = "немає констант FSR 2";
    else if (!out) why = "немає виходу";
    if (why) {
        if (InterlockedIncrement(&g_fallback) <= 5) LOG("міст: пропуск кадру (%s) — рідний FSR 2", why);
        return 0;
    }
    uint32_t rw = fr.cb[0], rh = fr.cb[1], dw = fr.cb[2], dh = fr.cb[3];
    if (!rw || !rh || !dw || !dh || rw > 16384 || dw > 16384) {
        if (InterlockedIncrement(&g_fallback) <= 5) LOG("міст: дивні розміри %ux%u→%ux%u", rw, rh, dw, dh);
        return 0;
    }
    ID3D12Device *dev = NULL;
    ID3D12GraphicsCommandList_GetDevice(cl, &IID_ID3D12Device, (void **)&dev);
    if (!dev) return 0;
    AcquireSRWLockExclusive(&g_ffx_lock);
    int ok = ffx_ensure(dev, dw, dh);
    ID3D12Device_Release(dev);
    if (!ok) { ReleaseSRWLockExclusive(&g_ffx_lock); return 0; }

    float f[64];
    memcpy(f, fr.cb, sizeof(f));
    LARGE_INTEGER now; QueryPerformanceCounter(&now);
    float dt_ms = g_last_qpc.QuadPart ? (float)((now.QuadPart - g_last_qpc.QuadPart) * 1000.0 / (double)g_qpf.QuadPart) : 16.6f;
    if (dt_ms <= 0.f || dt_ms > 200.f) dt_ms = 16.6f;
    g_last_qpc = now;

    struct ffxDispatchDescUpscale dd;
    memset(&dd, 0, sizeof(dd));
    dd.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
    dd.commandList = cl;
    dd.color = ffx_res(fr.color, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    dd.depth = ffx_res(depth, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    dd.motionVectors = ffx_res(fr.mv, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    if (g_cfg.reactive && fr.reactive) dd.reactive = ffx_res(fr.reactive, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    dd.output = ffx_res(out, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
    dd.jitterOffset.x = f[10];
    dd.jitterOffset.y = f[11];
    /* у константах FSR 2: масштаб векторів = scale / renderSize */
    dd.motionVectorScale.x = f[24] * (float)rw;
    dd.motionVectorScale.y = f[25] * (float)rh;
    dd.renderSize.width = rw; dd.renderSize.height = rh;
    dd.upscaleSize.width = dw; dd.upscaleSize.height = dh;
    dd.enableSharpening = g_cfg.sharpness > 0.0f;
    dd.sharpness = g_cfg.sharpness;
    dd.frameTimeDelta = dt_ms;
    dd.preExposure = f[28] > 0.0f ? f[28] : 1.0f;
    static uint32_t last_frame_index;
    dd.reset = fr.cb[7] < last_frame_index;   /* лічильник кадрів FSR 2 скинувся — монтажна склейка */
    last_frame_index = fr.cb[7];
    dd.cameraNear = g_cfg.cam_near;
    dd.cameraFar = g_cfg.cam_far;
    dd.cameraFovAngleVertical = (f[29] > 0.01f && f[29] < 10.f) ? 2.0f * atanf(f[29]) : 1.0f;
    dd.viewSpaceToMetersFactor = 1.0f;

    t_in_ffx = 1;
    ffxReturnCode_t rc = f_dispatch(&g_ctx, &dd.header);
    t_in_ffx = 0;
    ReleaseSRWLockExclusive(&g_ffx_lock);
    restore_state(cl, st);

    if (rc != FFX_API_RETURN_OK) {
        LOG("FFX: ffxDispatch = %u — вимикаю міст", rc);
        g_ffx_state = -1;
        return 0;   /* виконаємо рідний RCAS: картинка лишиться коректною */
    }
    InterlockedIncrement(&g_bridged);
    if (!g_logged_first) {
        g_logged_first = 1;
        ID3D12Resource *rr[5] = {fr.color, depth, fr.mv, out, fr.reactive};
        const char *nn[5] = {"колір", "глибина", "вектори", "вихід", "маска"};
        for (int i = 0; i < 5; i++) if (rr[i]) {
            D3D12_RESOURCE_DESC d; ID3D12Resource_GetDesc(rr[i], &d);
            LOG("міст: %s = %p %llux%u формат %d", nn[i], rr[i], (unsigned long long)d.Width, d.Height, d.Format);
        }
        LOG("міст: перший кадр через %s: %ux%u→%ux%u jitter=(%.3f,%.3f) mvScale=(%.3f,%.3f) preExp=%.3f fov=%.1f°",
            g_ffx_version, rw, rh, dw, dh, dd.jitterOffset.x, dd.jitterOffset.y, dd.motionVectorScale.x,
            dd.motionVectorScale.y, dd.preExposure, dd.cameraFovAngleVertical * 57.2958f);
    }
    DWORD t = GetTickCount();
    if (t - g_last_stat > 30000) {
        g_last_stat = t;
        LOG("міст: кадрів через FFX %ld, з рідним FSR 2 %ld", g_bridged, g_fallback);
    }
    return 1;
}

static void STDMETHODCALLTYPE h_SetDescriptorHeaps(ID3D12GraphicsCommandList *cl, UINT n, ID3D12DescriptorHeap *const *heaps)
{
    if (!t_in_ffx) {
        AcquireSRWLockExclusive(&g_cl_lock);
        cl_state *s = cl_get(cl);
        s->nheaps = n > 2 ? 2 : n;
        for (UINT i = 0; i < s->nheaps; i++) s->heaps[i] = heaps[i];
        ReleaseSRWLockExclusive(&g_cl_lock);
    }
    o_SetDescriptorHeaps(cl, n, heaps);
}

static void STDMETHODCALLTYPE h_Reset(ID3D12GraphicsCommandList *cl, ID3D12CommandAllocator *a, ID3D12PipelineState *pso)
{
    AcquireSRWLockExclusive(&g_cl_lock);
    cl_state *s = cl_get(cl);
    void *keep = s->cl;
    memset(s, 0, sizeof(*s));
    s->cl = keep;
    s->pso = pso;
    ReleaseSRWLockExclusive(&g_cl_lock);
    o_Reset(cl, a, pso);
}

static void STDMETHODCALLTYPE h_Dispatch(ID3D12GraphicsCommandList *cl, UINT x, UINT y, UINT z)
{
    if (t_in_ffx || !g_cfg.enable || g_ffx_state < 0) { o_Dispatch(cl, x, y, z); return; }
    cl_state st;
    AcquireSRWLockShared(&g_cl_lock);
    st = *cl_get(cl);
    ReleaseSRWLockShared(&g_cl_lock);
    uint64_t h = st.pso ? pso_hash(st.pso) : 0;
    switch (h) {
    case 0x77501c64360b3290ULL:   /* крок 1: глибина (t2), маска (u0) — початок кадру FSR 2 */
        AcquireSRWLockExclusive(&g_fr_lock);
        memset(&g_fr, 0, sizeof(g_fr));
        g_fr.cl = cl;
        g_fr.depth = table_find(&st, 1, DXGI_FORMAT_R32G8X24_TYPELESS, 0, 0, D_SRV);
        if (!g_fr.depth) g_fr.depth = table_find(&st, 1, DXGI_FORMAT_R32_TYPELESS, 0, 0, D_SRV);
        if (!g_fr.depth) g_fr.depth = table_find(&st, 1, DXGI_FORMAT_D32_FLOAT, 0, 0, D_SRV);
        g_fr.reactive = table_res(&st, 2, 0, D_UAV);
        ReleaseSRWLockExclusive(&g_fr_lock);
        break;
    case 0xfacf8f57b36f482fULL:   /* крок 2: вектори руху для FSR (u0) */
        AcquireSRWLockExclusive(&g_fr_lock);
        if (g_fr.cl == cl) g_fr.mv = table_res(&st, 2, 0, D_UAV);
        ReleaseSRWLockExclusive(&g_fr_lock);
        break;
    case 0x373b42588c9e5f23ULL:   /* крок 3: перетворена глибина (u0, R32_FLOAT) */
        AcquireSRWLockExclusive(&g_fr_lock);
        if (g_fr.cl == cl) g_fr.depth_conv = table_res(&st, 2, 0, D_UAV);
        ReleaseSRWLockExclusive(&g_fr_lock);
        break;
    case 0x6c5793de6ada1e41ULL:   /* крок 4: колір кадру в рендер-роздільності (t0) */
        AcquireSRWLockExclusive(&g_fr_lock);
        if (g_fr.cl == cl) g_fr.color = table_find(&st, 1, DXGI_FORMAT_R11G11B10_FLOAT, 0, 0, D_SRV);
        ReleaseSRWLockExclusive(&g_fr_lock);
        break;
    case 0xc669a9db25ea4759ULL: { /* крок 9: накопичення — константи FSR 2 (b0) */
        uint32_t cb[64];
        int ok = table_cbv(&st, 0, 0, cb, 64);
        AcquireSRWLockExclusive(&g_fr_lock);
        if (g_fr.cl == cl && ok) { memcpy(g_fr.cb, cb, sizeof(cb)); g_fr.have_cb = 1; }
        int skip = g_cfg.skip_accumulate && g_ffx_state == 1 && g_fr.cl == cl && ok && g_fr.color && g_fr.mv;
        g_fr.accumulate_skipped = skip;
        ReleaseSRWLockExclusive(&g_fr_lock);
        if (skip) return;
        break;
    }
    case 0xc7e4d3dc11696abfULL:   /* крок 10: RCAS → тут FSR 4 */
        if (bridge_rcas(cl, &st)) return;
        break;
    default:
        break;
    }
    o_Dispatch(cl, x, y, z);
}

static int hook(void *target, void *detour, void **orig, const char *name)
{
    if (!target) { LOG("hook %s: немає адреси", name); return 0; }
    MH_STATUS s = MH_CreateHook(target, detour, orig);
    if (s == MH_ERROR_ALREADY_CREATED) return 1;
    if (s != MH_OK) { LOG("hook %s: MH_CreateHook=%d", name, (int)s); return 0; }
    return 1;
}

typedef HRESULT (WINAPI *PFN_D3D12CreateDevice_)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);

static DWORD WINAPI init_thread(void *arg)
{
    (void)arg;
    HMODULE d3d12 = LoadLibraryW(L"d3d12.dll");
    PFN_D3D12CreateDevice_ create = d3d12 ? (PFN_D3D12CreateDevice_)GetProcAddress(d3d12, "D3D12CreateDevice") : NULL;
    p_CreateRSDeserializer = d3d12 ? (PFN_CreateRSDeserializer)GetProcAddress(d3d12, "D3D12CreateRootSignatureDeserializer") : NULL;
    if (!create) { LOG("d3d12.dll / D3D12CreateDevice не знайдено"); return 0; }

    ID3D12Device *dev = NULL;
    HRESULT hr = create(NULL, D3D_FEATURE_LEVEL_11_0, &IID_ID3D12Device, (void **)&dev);
    if (FAILED(hr) || !dev) { LOG("D3D12CreateDevice (тимчасовий) = %08lx", (unsigned long)hr); return 0; }
    for (int t = 0; t < 4; t++) g_inc[t] = ID3D12Device_GetDescriptorHandleIncrementSize(dev, (D3D12_DESCRIPTOR_HEAP_TYPE)t);

    ID3D12CommandAllocator *alloc = NULL;
    ID3D12GraphicsCommandList *cl = NULL;
    ID3D12Device_CreateCommandAllocator(dev, D3D12_COMMAND_LIST_TYPE_DIRECT, &IID_ID3D12CommandAllocator, (void **)&alloc);
    if (alloc) ID3D12Device_CreateCommandList(dev, 0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, NULL, &IID_ID3D12GraphicsCommandList, (void **)&cl);

    /* тимчасовий буфер у UPLOAD-купі — заради адреси функції Map */
    ID3D12Resource *buf = NULL;
    D3D12_HEAP_PROPERTIES hp = {0}; hp.Type = D3D12_HEAP_TYPE_UPLOAD;
    D3D12_RESOURCE_DESC rd = {0};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; rd.Width = 256; rd.Height = 1; rd.DepthOrArraySize = 1;
    rd.MipLevels = 1; rd.SampleDesc.Count = 1; rd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Device_CreateCommittedResource(dev, &hp, D3D12_HEAP_FLAG_NONE, &rd, D3D12_RESOURCE_STATE_GENERIC_READ, NULL, &IID_ID3D12Resource, (void **)&buf);

    hook((void *)dev->lpVtbl->CreateComputePipelineState, (void *)h_CreateComputePSO, (void **)&o_CreateComputePSO, "CreateComputePipelineState");
    hook((void *)dev->lpVtbl->CreateRootSignature, (void *)h_CreateRootSignature, (void **)&o_CreateRootSignature, "CreateRootSignature");
    hook((void *)dev->lpVtbl->CreateDescriptorHeap, (void *)h_CreateDescriptorHeap, (void **)&o_CreateDescriptorHeap, "CreateDescriptorHeap");
    hook((void *)dev->lpVtbl->CreateShaderResourceView, (void *)h_CreateSRV, (void **)&o_CreateSRV, "CreateShaderResourceView");
    hook((void *)dev->lpVtbl->CreateUnorderedAccessView, (void *)h_CreateUAV, (void **)&o_CreateUAV, "CreateUnorderedAccessView");
    hook((void *)dev->lpVtbl->CreateConstantBufferView, (void *)h_CreateCBV, (void **)&o_CreateCBV, "CreateConstantBufferView");
    hook((void *)dev->lpVtbl->CopyDescriptorsSimple, (void *)h_CopyDescriptorsSimple, (void **)&o_CopyDescriptorsSimple, "CopyDescriptorsSimple");
    hook((void *)dev->lpVtbl->CopyDescriptors, (void *)h_CopyDescriptors, (void **)&o_CopyDescriptors, "CopyDescriptors");
    if (buf) hook((void *)buf->lpVtbl->Map, (void *)h_Map, (void **)&o_Map, "Resource::Map");

    ID3D12Device2 *dev2 = NULL;
    if (SUCCEEDED(ID3D12Device_QueryInterface(dev, &IID_ID3D12Device2, (void **)&dev2)) && dev2) {
        hook((void *)dev2->lpVtbl->CreatePipelineState, (void *)h_CreatePSO2, (void **)&o_CreatePSO2, "CreatePipelineState");
        ID3D12PipelineLibrary *lib = NULL;
        if (SUCCEEDED(ID3D12Device2_CreatePipelineLibrary(dev2, NULL, 0, &IID_ID3D12PipelineLibrary, (void **)&lib)) && lib) {
            hook((void *)lib->lpVtbl->LoadComputePipeline, (void *)h_LoadComputePipeline, (void **)&o_LoadComputePipeline, "LoadComputePipeline");
            ID3D12PipelineLibrary_Release(lib);
        }
        ID3D12Device2_Release(dev2);
    }
    if (cl) {
        hook((void *)cl->lpVtbl->SetPipelineState, (void *)h_SetPipelineState, (void **)&o_SetPipelineState, "SetPipelineState");
        hook((void *)cl->lpVtbl->SetDescriptorHeaps, (void *)h_SetDescriptorHeaps, (void **)&o_SetDescriptorHeaps, "SetDescriptorHeaps");
        hook((void *)cl->lpVtbl->Reset, (void *)h_Reset, (void **)&o_Reset, "Reset");
        hook((void *)cl->lpVtbl->SetGraphicsRootSignature, (void *)h_SetGraphicsRootSignature, (void **)&o_SetGraphicsRootSignature, "SetGraphicsRootSignature");
        hook((void *)cl->lpVtbl->SetGraphicsRootDescriptorTable, (void *)h_SetGraphicsRootDescriptorTable, (void **)&o_SetGraphicsRootDescriptorTable, "SetGraphicsRootDescriptorTable");
        hook((void *)cl->lpVtbl->Dispatch, (void *)h_Dispatch, (void **)&o_Dispatch, "Dispatch");
        hook((void *)cl->lpVtbl->SetComputeRootSignature, (void *)h_SetComputeRootSignature, (void **)&o_SetComputeRootSignature, "SetComputeRootSignature");
        hook((void *)cl->lpVtbl->SetComputeRootDescriptorTable, (void *)h_SetComputeRootDescriptorTable, (void **)&o_SetComputeRootDescriptorTable, "SetComputeRootDescriptorTable");
        hook((void *)cl->lpVtbl->SetComputeRoot32BitConstants, (void *)h_SetComputeRoot32BitConstants, (void **)&o_SetComputeRoot32BitConstants, "SetComputeRoot32BitConstants");
        hook((void *)cl->lpVtbl->SetComputeRootConstantBufferView, (void *)h_SetComputeRootCBV, (void **)&o_SetComputeRootCBV, "SetComputeRootConstantBufferView");
    }
    MH_STATUS s = MH_EnableHook(MH_ALL_HOOKS);
    LOG("MH_EnableHook = %d; deserializer=%p; inc srv=%u", (int)s, (void *)p_CreateRSDeserializer, g_inc[0]);

    if (buf) ID3D12Resource_Release(buf);
    if (cl) ID3D12GraphicsCommandList_Release(cl);
    if (alloc) ID3D12CommandAllocator_Release(alloc);
    ID3D12Device_Release(dev);
    return 0;
}

/* ------------------------------------------------------------------ проксі dinput8 */

static HMODULE g_real;
static FARPROC real_proc(const char *name)
{
    if (!g_real) {
        wchar_t path[MAX_PATH];
        GetSystemDirectoryW(path, MAX_PATH);
        wcscat(path, L"\\dinput8.dll");
        g_real = LoadLibraryW(path);
    }
    return g_real ? GetProcAddress(g_real, name) : NULL;
}

typedef HRESULT (WINAPI *PFN_DI8Create)(HINSTANCE, DWORD, REFIID, LPVOID *, LPUNKNOWN);
HRESULT WINAPI DirectInput8Create(HINSTANCE h, DWORD v, REFIID r, LPVOID *o, LPUNKNOWN u)
{
    PFN_DI8Create f = (PFN_DI8Create)real_proc("DirectInput8Create");
    return f ? f(h, v, r, o, u) : E_FAIL;
}
typedef HRESULT (WINAPI *PFN_Void)(void);
typedef HRESULT (WINAPI *PFN_GetClassObject)(REFCLSID, REFIID, LPVOID *);
typedef LPCVOID (WINAPI *PFN_GetDf)(void);
HRESULT WINAPI proxy_DllCanUnloadNow(void) { PFN_Void f = (PFN_Void)real_proc("DllCanUnloadNow"); return f ? f() : S_FALSE; }
HRESULT WINAPI proxy_DllGetClassObject(REFCLSID c, REFIID r, LPVOID *o)
{
    PFN_GetClassObject f = (PFN_GetClassObject)real_proc("DllGetClassObject");
    return f ? f(c, r, o) : CLASS_E_CLASSNOTAVAILABLE;
}
HRESULT WINAPI proxy_DllRegisterServer(void) { PFN_Void f = (PFN_Void)real_proc("DllRegisterServer"); return f ? f() : E_FAIL; }
HRESULT WINAPI proxy_DllUnregisterServer(void) { PFN_Void f = (PFN_Void)real_proc("DllUnregisterServer"); return f ? f() : E_FAIL; }
LPCVOID WINAPI proxy_GetdfDIJoystick(void) { PFN_GetDf f = (PFN_GetDf)real_proc("GetdfDIJoystick"); return f ? f() : NULL; }

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_cs);
        g_t0 = GetTickCount();
        g_pso = (pso_ent *)VirtualAlloc(NULL, sizeof(pso_ent) * PSO_CAP, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        g_desc = (desc_ent *)VirtualAlloc(NULL, sizeof(desc_ent) * DESC_CAP, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        g_rs = (rs_info *)VirtualAlloc(NULL, sizeof(rs_info) * RS_CAP, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        g_maps = (map_info *)VirtualAlloc(NULL, sizeof(map_info) * MAP_CAP, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        open_log();
        LOG("AutoFPS FSR2→FSR4 міст (етап 3), збірка " __DATE__ " " __TIME__);
        load_cfg();
        QueryPerformanceFrequency(&g_qpf);
        if (!g_pso || !g_desc || !g_rs || !g_maps) { LOG("немає пам'яті для таблиць"); return TRUE; }
        if (MH_Initialize() != MH_OK) { LOG("MH_Initialize помилка"); return TRUE; }
        HANDLE t = CreateThread(NULL, 0, init_thread, NULL, 0, NULL);
        if (t) CloseHandle(t);
    }
    return TRUE;
}
