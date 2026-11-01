/*
 * AutoFPS FSR2→FSR4 bridge — етап 2: ДІАГНОСТИКА РЕСУРСІВ FSR 2.
 *
 * Етап 1 знайшов кроки FSR 2 у RE4 (за хешами шейдерів). Цей етап з'ясовує, ЩО саме подається
 * в ці кроки: для кожного Dispatch кроку FSR 2 пише в autofps_fsr_diag.log
 *   - розкладку кореневої сигнатури (таблиці дескрипторів: SRV/UAV/CBV, регістри);
 *   - ресурси в кожній таблиці: розмір, формат, тип (текстура/буфер), вказівник;
 *   - вміст константних буферів (там зсув камери/jitter, розміри, масштаб векторів руху).
 * Картинку не змінює.
 *
 * Ліцензія: BSD-3-Clause (як AutoFPS). MinHook — BSD-2-Clause, DirectX-Headers — MIT.
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
    wcscat(path, L"autofps_fsr_diag.log");
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
typedef struct { void *cl; void *rs; void *pso; uint64_t tables[16]; } cl_state;
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
    AcquireSRWLockExclusive(&g_cl_lock); cl_get(cl)->pso = pso; ReleaseSRWLockExclusive(&g_cl_lock);
    o_SetPipelineState(cl, pso);
}

static void STDMETHODCALLTYPE h_SetComputeRootSignature(ID3D12GraphicsCommandList *cl, ID3D12RootSignature *rs)
{
    AcquireSRWLockExclusive(&g_cl_lock); cl_get(cl)->rs = rs; ReleaseSRWLockExclusive(&g_cl_lock);
    o_SetComputeRootSignature(cl, rs);
}

static void STDMETHODCALLTYPE h_SetComputeRootDescriptorTable(ID3D12GraphicsCommandList *cl, UINT idx, D3D12_GPU_DESCRIPTOR_HANDLE h)
{
    if (idx < 16) { AcquireSRWLockExclusive(&g_cl_lock); cl_get(cl)->tables[idx] = h.ptr; ReleaseSRWLockExclusive(&g_cl_lock); }
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

static volatile LONG g_dumped[8];

static void STDMETHODCALLTYPE h_Dispatch(ID3D12GraphicsCommandList *cl, UINT x, UINT y, UINT z)
{
    int w = cur_window();
    if (w >= 0) {
        cl_state st;
        AcquireSRWLockShared(&g_cl_lock);
        st = *cl_get(cl);
        ReleaseSRWLockShared(&g_cl_lock);
        const char *nm = st.pso ? known_name(pso_hash(st.pso)) : NULL;
        if (nm && InterlockedIncrement(&g_dumped[w]) <= 44)   /* ~4 кадри FSR 2 на вікно */
            dump_dispatch(&st, nm, x, y, z);
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
        LOG("AutoFPS FSR2-міст: діагностика ресурсів (етап 2), збірка " __DATE__ " " __TIME__);
        if (!g_pso || !g_desc || !g_rs || !g_maps) { LOG("немає пам'яті для таблиць"); return TRUE; }
        if (MH_Initialize() != MH_OK) { LOG("MH_Initialize помилка"); return TRUE; }
        HANDLE t = CreateThread(NULL, 0, init_thread, NULL, 0, NULL);
        if (t) CloseHandle(t);
    }
    return TRUE;
}
