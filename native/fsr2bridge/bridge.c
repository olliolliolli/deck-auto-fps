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
static volatile LONG g_known_psos;   /* скільки кроків накопичення FSR 2 знайдено (автоматично або за відомим хешем) */
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

/* PSO → хеш шейдера + клас кроку FSR 2 (визначається автоматично під час гри) */
enum { PC_UNKNOWN = 0, PC_OTHER, PC_ACCUM, PC_RCAS, PC_LUMA, PC_PREP, PC_MV, PC_DCONV };
static const char *const g_pc_name[] = {"?", "інше", "накопичення", "RCAS", "яскравість/колір", "підготовка", "вектори руху", "глибина"};
typedef struct { void *key; uint64_t hash; uint8_t cls, checks; } pso_ent;
#define PSO_CAP (1u << 18)
#define PSO_MAX_CHECKS 24
static pso_ent *g_pso;
static SRWLOCK g_pso_lock = SRWLOCK_INIT;

static void pso_put(void *k, uint64_t h, uint8_t cls)
{
    AcquireSRWLockExclusive(&g_pso_lock);
    uint32_t i = mix((uint64_t)(uintptr_t)k, PSO_CAP);
    for (uint32_t n = 0; n < PSO_CAP; n++, i = (i + 1) & (PSO_CAP - 1))
        if (!g_pso[i].key || g_pso[i].key == k) { g_pso[i].key = k; g_pso[i].hash = h; g_pso[i].cls = cls; g_pso[i].checks = 0; break; }
    ReleaseSRWLockExclusive(&g_pso_lock);
}

static int pso_get(void *k, pso_ent *out)
{
    int ok = 0;
    AcquireSRWLockShared(&g_pso_lock);
    uint32_t i = mix((uint64_t)(uintptr_t)k, PSO_CAP);
    for (uint32_t n = 0; n < 64; n++, i = (i + 1) & (PSO_CAP - 1)) {
        if (g_pso[i].key == k) { *out = g_pso[i]; ok = 1; break; }
        if (!g_pso[i].key) break;
    }
    ReleaseSRWLockShared(&g_pso_lock);
    return ok;
}

/* cls >= 0 — встановити клас; cls < 0 — ще одна невдала перевірка (після PSO_MAX_CHECKS → «інше») */
static void pso_mark(void *k, int cls)
{
    AcquireSRWLockExclusive(&g_pso_lock);
    uint32_t i = mix((uint64_t)(uintptr_t)k, PSO_CAP);
    for (uint32_t n = 0; n < 64; n++, i = (i + 1) & (PSO_CAP - 1)) {
        if (g_pso[i].key == k) {
            if (cls >= 0) g_pso[i].cls = (uint8_t)cls;
            else if (g_pso[i].cls == PC_UNKNOWN && ++g_pso[i].checks >= PSO_MAX_CHECKS) g_pso[i].cls = PC_OTHER;
            break;
        }
        if (!g_pso[i].key) break;
    }
    ReleaseSRWLockExclusive(&g_pso_lock);
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

/* таблиця розбита на смуги з окремими блокуваннями: рушій створює й копіює дескриптори з
   багатьох потоків одночасно, одне спільне блокування давало підвисання */
#define DESC_STRIPES 128u
#define DESC_SUB (DESC_CAP / DESC_STRIPES)
static SRWLOCK g_dstripe[DESC_STRIPES];

static void desc_put(size_t k, const desc_ent *v)
{
    uint64_t x = (uint64_t)k; x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33;
    uint32_t st = (uint32_t)x & (DESC_STRIPES - 1), i = (uint32_t)(x >> 7) & (DESC_SUB - 1);
    desc_ent *b = g_desc + (size_t)st * DESC_SUB;
    AcquireSRWLockExclusive(&g_dstripe[st]);
    for (uint32_t n = 0; n < 1024; n++, i = (i + 1) & (DESC_SUB - 1)) {
        if (b[i].key == 0 || b[i].key == k) { b[i] = *v; b[i].key = k; break; }
    }
    ReleaseSRWLockExclusive(&g_dstripe[st]);
}

static int desc_get(size_t k, desc_ent *out)
{
    uint64_t x = (uint64_t)k; x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33;
    uint32_t st = (uint32_t)x & (DESC_STRIPES - 1), i = (uint32_t)(x >> 7) & (DESC_SUB - 1);
    desc_ent *b = g_desc + (size_t)st * DESC_SUB;
    int ok = 0;
    AcquireSRWLockShared(&g_dstripe[st]);
    for (uint32_t n = 0; n < 1024; n++, i = (i + 1) & (DESC_SUB - 1)) {
        if (b[i].key == k) { *out = b[i]; ok = 1; break; }
        if (b[i].key == 0) break;
    }
    ReleaseSRWLockShared(&g_dstripe[st]);
    return ok;
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
    uint8_t cls = PC_UNKNOWN;
    switch (h) {   /* RE4: відомі кроки — одразу; решту ігор/версій визначаємо під час гри */
    case 0x77501c64360b3290ULL: cls = PC_PREP; break;
    case 0xfacf8f57b36f482fULL: cls = PC_MV; break;
    case 0x373b42588c9e5f23ULL: cls = PC_DCONV; break;
    case 0x6c5793de6ada1e41ULL: cls = PC_LUMA; break;
    case 0xc669a9db25ea4759ULL: cls = PC_ACCUM; break;
    case 0xc7e4d3dc11696abfULL: cls = PC_RCAS; break;
    default: break;
    }
    pso_put(pso, h, cls);
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
    desc_put(h.ptr, &e);
}

static void STDMETHODCALLTYPE h_CreateUAV(ID3D12Device *dev, ID3D12Resource *res, ID3D12Resource *ctr, const D3D12_UNORDERED_ACCESS_VIEW_DESC *d, D3D12_CPU_DESCRIPTOR_HANDLE h)
{
    o_CreateUAV(dev, res, ctr, d, h);
    desc_ent e = {0};
    e.kind = D_UAV; e.res = res;
    if (d) { e.fmt = (uint16_t)d->Format; e.dim = (uint16_t)d->ViewDimension; }
    desc_put(h.ptr, &e);
}

static void STDMETHODCALLTYPE h_CreateCBV(ID3D12Device *dev, const D3D12_CONSTANT_BUFFER_VIEW_DESC *d, D3D12_CPU_DESCRIPTOR_HANDLE h)
{
    o_CreateCBV(dev, d, h);
    desc_ent e = {0};
    e.kind = D_CBV;
    if (d) { e.va = d->BufferLocation; e.size = d->SizeInBytes; }
    desc_put(h.ptr, &e);
}

static void copy_range_locked(size_t dst, size_t src, UINT n, UINT inc)
{
    for (UINT i = 0; i < n; i++) {
        desc_ent e;
        if (desc_get(src + (size_t)i * inc, &e)) desc_put(dst + (size_t)i * inc, &e);
        else { desc_ent z = {0}; desc_put(dst + (size_t)i * inc, &z); }
    }
}

static void STDMETHODCALLTYPE h_CopyDescriptorsSimple(ID3D12Device *dev, UINT n, D3D12_CPU_DESCRIPTOR_HANDLE dst, D3D12_CPU_DESCRIPTOR_HANDLE src, D3D12_DESCRIPTOR_HEAP_TYPE type)
{
    o_CopyDescriptorsSimple(dev, n, dst, src, type);
    if (type != D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV) return;
    copy_range_locked(dst.ptr, src.ptr, n, g_inc[type]);
}

static void STDMETHODCALLTYPE h_CopyDescriptors(ID3D12Device *dev, UINT nd, const D3D12_CPU_DESCRIPTOR_HANDLE *ds, const UINT *dsz,
                                                UINT ns, const D3D12_CPU_DESCRIPTOR_HANDLE *ss, const UINT *ssz, D3D12_DESCRIPTOR_HEAP_TYPE type)
{
    o_CopyDescriptors(dev, nd, ds, dsz, ns, ss, ssz, type);
    if (type != D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV) return;
    UINT inc = g_inc[type];
    UINT di = 0, doff = 0, si = 0, soff = 0;
    while (di < nd && si < ns) {
        UINT dn = dsz ? dsz[di] : 1, sn = ssz ? ssz[si] : 1;
        copy_range_locked(ds[di].ptr + (size_t)doff * inc, ss[si].ptr + (size_t)soff * inc, 1, inc);
        if (++doff >= dn) { di++; doff = 0; }
        if (++soff >= sn) { si++; soff = 0; }
    }
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
                ok = desc_get(cpu + (size_t)(start + j) * inc, &e);
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


static volatile LONG g_bridged, g_fallback;   /* статистика (визначено тут, бо потрібна й нижче) */
static void write_status(const char *state);

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
    float mvx, mvy;      /* масштаб векторів руху: множник до renderSize (1 = вектори в UV) */
    float jx, jy;        /* знак/множник зсуву камери */
    int jitter_src;      /* 0 — з констант FSR 2 [10,11] */
    int sanitize;        /* 1 — очищати колір/глибину/вектори від NaN/Inf перед FSR 4 */
    int exposure;        /* 0 — автоекспозиція FFX, 1 — експозиція з FSR 2 гри (1x1) */
} g_cfg = {1, FFX_UPSCALE_ENABLE_HIGH_DYNAMIC_RANGE | FFX_UPSCALE_ENABLE_AUTO_EXPOSURE |
              FFX_UPSCALE_ENABLE_DEPTH_INVERTED | FFX_UPSCALE_ENABLE_DEPTH_INFINITE,
           0, 0, 1, 0.5f, 0.1f, 10000.0f, "4", 1.0f, 1.0f, 1.0f, 1.0f, 0, 1, 0};
static FILETIME g_cfg_mtime;
static uint32_t g_ctx_w, g_ctx_h;   /* оголошено тут, визначено нижче (FFX) */
static volatile LONG g_cfg_changed;

static void load_cfg(void)
{
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, path, MAX_PATH);
    while (n > 0 && path[n - 1] != L'\\' && path[n - 1] != L'/') n--;
    path[n] = 0;
    wcscat(path, L"autofps_fsr.ini");
    WIN32_FILE_ATTRIBUTE_DATA fa;
    if (GetFileAttributesExW(path, GetFileExInfoStandard, &fa)) g_cfg_mtime = fa.ftLastWriteTime;
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
        else if (!strcmp(k, "version")) { snprintf(g_cfg.want, sizeof(g_cfg.want), "%s", v); }
        else if (!strcmp(k, "mv_x")) g_cfg.mvx = (float)atof(v);
        else if (!strcmp(k, "mv_y")) g_cfg.mvy = (float)atof(v);
        else if (!strcmp(k, "jitter_x")) g_cfg.jx = (float)atof(v);
        else if (!strcmp(k, "jitter_y")) g_cfg.jy = (float)atof(v);
        else if (!strcmp(k, "sanitize")) g_cfg.sanitize = atoi(v);
        else if (!strcmp(k, "exposure")) g_cfg.exposure = !strcmp(v, "game") || !strcmp(v, "1");
    }
    fclose(f);
    LOG("налаштування: enable=%d flags=0x%x depth_conv=%d reactive=%d skip_accumulate=%d sharpness=%.2f near=%g far=%g version=%s mv=(%g,%g) jitter=(%g,%g) sanitize=%d exposure=%s",
        g_cfg.enable, g_cfg.flags, g_cfg.depth_conv, g_cfg.reactive, g_cfg.skip_accumulate, g_cfg.sharpness,
        g_cfg.cam_near, g_cfg.cam_far, g_cfg.want, g_cfg.mvx, g_cfg.mvy, g_cfg.jx, g_cfg.jy, g_cfg.sanitize,
        g_cfg.exposure ? "game" : "auto");
}

/* перечитує autofps_fsr.ini, якщо файл змінився (налаштування на льоту, під час гри) */
static DWORD WINAPI cfg_watch_thread(void *arg)
{
    (void)arg;
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, path, MAX_PATH);
    while (n > 0 && path[n - 1] != L'\\' && path[n - 1] != L'/') n--;
    path[n] = 0;
    wcscat(path, L"autofps_fsr.ini");
    int warned = 0;
    for (;;) {
        Sleep(2000);
        if (!warned && GetTickCount() - g_t0 > 120000 && g_bridged == 0) {
            warned = 1;
            if (g_known_psos == 0) { LOG("FSR 2 у кадрі не знайдено (увімкни FSR 2 у налаштуваннях графіки гри)"); write_status("fsr2_off"); }
            else { LOG("FSR 2 знайдено, але кадри не вдалося передати в FFX — працює рідний FSR 2"); write_status("no_fsr2_shaders"); }
        }
        WIN32_FILE_ATTRIBUTE_DATA fa;
        if (GetFileAttributesExW(path, GetFileExInfoStandard, &fa) &&
            CompareFileTime(&fa.ftLastWriteTime, &g_cfg_mtime) != 0) {
            uint32_t old_flags = g_cfg.flags ^ (g_cfg.exposure ? 0x80000000u : 0);
            char old_want[32]; memcpy(old_want, g_cfg.want, sizeof(old_want));
            load_cfg();
            LOG("налаштування оновлено на льоту");
            if (old_flags != (g_cfg.flags ^ (g_cfg.exposure ? 0x80000000u : 0)) || strcmp(old_want, g_cfg.want))
                g_ctx_w = 0;  /* перестворити контекст */
            InterlockedExchange(&g_cfg_changed, 1);
        }
    }
    return 0;
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
    /* перевірка FFX (DEBUG_CHECKING) повторює ті самі попередження щокадру — кожне пишемо до 3 разів */
    static uint64_t seen[64]; static uint8_t cnt[64]; static int nseen;
    char buf[512];
    WideCharToMultiByte(CP_UTF8, 0, msg, -1, buf, sizeof(buf), NULL, NULL);
    uint64_t hsh = fnv1a(buf, strlen(buf));
    int i;
    for (i = 0; i < nseen; i++) if (seen[i] == hsh) break;
    if (i == nseen) { if (nseen >= 64) return; seen[nseen++] = hsh; }
    if (cnt[i] >= 3) return;
    cnt[i]++;
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
    if (!m) { LOG("amd_fidelityfx_dx12.dll не знайдено поруч з грою — міст вимкнено"); write_status("no_ffx"); return 0; }
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
    uint64_t pick = 0; int best = -100000;
    for (uint64_t i = 0; i < count; i++) {
        const char *nm = names[i] ? names[i] : "?";
        LOG("FFX: доступна версія %llu: %s (id=%llx)", (unsigned long long)i, nm, (unsigned long long)ids[i]);
        const char *dg = nm;
        while (*dg && (*dg < '0' || *dg > '9')) dg++;          /* «HLX FSR 4 (4.1.2)» → 4 */
        int major = atoi(dg);
        int hlx = strstr(nm, "HLX") != NULL || strstr(nm, "Helix") != NULL;
        int score = major * 100 - (int)i;   /* при рівності — перша в списку (найновіша) */
        if (g_cfg.want[0] == 'h') { if (hlx) score += 5000; }
        else if (hlx) score -= 3000;         /* HLX — лише коли його вибрали явно */
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
    if (g_cfg.exposure) cd.flags &= ~(uint32_t)FFX_UPSCALE_ENABLE_AUTO_EXPOSURE;   /* експозиція — з FSR 2 гри */
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
        write_status("ffx_failed");
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
    ID3D12Resource *color, *depth, *depth_conv, *mv, *reactive, *exposure;
    uint32_t cb[64];
    int have_cb;
    int accumulate_skipped;
    ID3D12Resource *mv_cand;     /* останній кандидат у вектори руху до кроку яскравості */
} frame_state;
/* розміри FSR 2, вивчені з констант накопичення (для автовизначення інших кроків) */
static volatile uint32_t g_rw, g_rh, g_dw, g_dh;
/* стан FSR 2 — окремо для кожного командного списку: рушій записує кілька кадрів паралельно */
#define FR_CAP 64
static frame_state g_frs[FR_CAP];
static SRWLOCK g_fr_lock = SRWLOCK_INIT;
static frame_state *fr_get(void *cl)   /* під g_fr_lock (exclusive) */
{
    for (int i = 0; i < FR_CAP; i++) if (g_frs[i].cl == cl) return &g_frs[i];
    for (int i = 0; i < FR_CAP; i++) if (!g_frs[i].cl) { g_frs[i].cl = cl; return &g_frs[i]; }
    static int rr; rr = (rr + 1) % FR_CAP;
    memset(&g_frs[rr], 0, sizeof(frame_state)); g_frs[rr].cl = cl;
    return &g_frs[rr];
}
static volatile LONG g_why[8];

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
    ok = desc_get(cpu + (size_t)(start + reg_index) * inc, &e);
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

/* глибина гри: D32S8 (R32G8X24_TYPELESS) у таблиці SRV (param 1) або в «змішаній» (param 2) */
static ID3D12Resource *find_depth(cl_state *st)
{
    ID3D12Resource *r = table_find(st, 1, DXGI_FORMAT_R32G8X24_TYPELESS, 0, 0, D_SRV);
    if (!r) r = table_find(st, 2, DXGI_FORMAT_R32G8X24_TYPELESS, 0, 0, D_SRV);
    return r;
}

/* ресурс потрібного виду/формату/розміру в будь-якій таблиці дескрипторів (fmts == NULL — будь-який формат) */
static ID3D12Resource *find_res(cl_state *st, int kind, const DXGI_FORMAT *fmts, int nf, uint32_t w, uint32_t h)
{
    rs_info *ri = rs_find(st->rs);
    if (!ri) return NULL;
    for (int f = 0; f < (fmts ? nf : 1); f++)
        for (uint32_t p = 0; p < ri->nparams && p < 16; p++) {
            param_info *pi = &ri->p[p];
            if (pi->type != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE || !st->tables[p]) continue;
            for (uint32_t i = 0; i < 32; i++) {
                ID3D12Resource *r = table_res(st, p, i, kind);
                if (!r) continue;
                D3D12_RESOURCE_DESC d;
                ID3D12Resource_GetDesc(r, &d);
                if (d.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D) continue;
                if (fmts && d.Format != fmts[f]) continue;
                if (w && (d.Width != w || d.Height != h)) continue;
                return r;
            }
        }
    return NULL;
}

static const DXGI_FORMAT k_depth_fmts[] = {DXGI_FORMAT_R32G8X24_TYPELESS, DXGI_FORMAT_R32_TYPELESS, DXGI_FORMAT_D32_FLOAT_S8X24_UINT, DXGI_FORMAT_D32_FLOAT};
static const DXGI_FORMAT k_color_fmts[] = {DXGI_FORMAT_R11G11B10_FLOAT, DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_R10G10B10A2_UNORM, DXGI_FORMAT_R8G8B8A8_UNORM};
static const DXGI_FORMAT k_mv_fmts[] = {DXGI_FORMAT_R16G16_FLOAT};
static const DXGI_FORMAT k_exp_fmts[] = {DXGI_FORMAT_R32G32_FLOAT};

static ID3D12Resource *find_depth_any(cl_state *st, uint32_t w, uint32_t h)
{
    ID3D12Resource *r = w ? find_res(st, D_SRV, k_depth_fmts, 4, w, h) : NULL;
    return r ? r : find_depth(st);
}

static int table_cbv(cl_state *st, uint32_t param, uint32_t reg_index, uint32_t *out, uint32_t n)
{
    rs_info *ri = rs_find(st->rs);
    if (!ri || param >= ri->nparams) return 0;
    size_t cpu; uint32_t inc;
    if (!gpu_to_cpu(st->tables[param], &cpu, &inc)) return 0;
    desc_ent e; int ok;
    ok = desc_get(cpu + (size_t)reg_index * inc, &e);
    if (!ok || e.kind != D_CBV) return 0;
    const uint8_t *p = va_to_cpu(e.va, n * 4);
    if (!p) return 0;
    memcpy(out, p, n * 4);
    return 1;
}

/* константи FSR 2 (розкладка Capcom/FSR 2.0): [0,1] рендер, [2,3] екран (int), [8,9] = 1/екран (float);
   крок накопичення запускається групами 8x8 на розмір екрана */
static int fsr2_cb_ok(const uint32_t *cb, UINT x, UINT y)
{
    uint32_t rw = cb[0], rh = cb[1], dw = cb[2], dh = cb[3];
    if (rw < 64 || rh < 64 || dw > 8192 || dh > 8192 || dw < rw || dh < rh) return 0;
    if (x != (dw + 7) / 8 || y != (dh + 7) / 8) return 0;
    float f8, f9;
    memcpy(&f8, &cb[8], 4); memcpy(&f9, &cb[9], 4);
    return fabsf(f8 * (float)dw - 1.0f) < 1e-3f && fabsf(f9 * (float)dh - 1.0f) < 1e-3f;
}

static int fsr2_cb_find(cl_state *st, UINT x, UINT y, uint32_t *cb)
{
    rs_info *ri = rs_find(st->rs);
    if (!ri) return 0;
    for (uint32_t p = 0; p < ri->nparams && p < 16; p++) {
        param_info *pi = &ri->p[p];
        if (pi->type != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE || pi->nranges < 1 || !st->tables[p]) continue;
        if (pi->r[0].type != D3D12_DESCRIPTOR_RANGE_TYPE_CBV) continue;
        for (uint32_t i = 0; i < 2 && i < pi->r[0].num; i++)
            if (table_cbv(st, p, i, cb, 64) && fsr2_cb_ok(cb, x, y)) return 1;
    }
    return 0;
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
    case DXGI_FORMAT_R32G32_FLOAT: x.description.format = FFX_API_SURFACE_FORMAT_R32G32_FLOAT; break;
    case DXGI_FORMAT_R32_FLOAT: case DXGI_FORMAT_D32_FLOAT: case DXGI_FORMAT_R32_TYPELESS: x.description.format = FFX_API_SURFACE_FORMAT_R32_FLOAT; break;
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
    else if (d.Format == DXGI_FORMAT_D32_FLOAT || d.Format == DXGI_FORMAT_R32_TYPELESS)
        x.description.usage = FFX_API_RESOURCE_USAGE_DEPTHTARGET;
    if (d.Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS) x.description.usage |= FFX_API_RESOURCE_USAGE_UAV;
    if (d.Flags & D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET) x.description.usage |= FFX_API_RESOURCE_USAGE_RENDERTARGET;
    return x;
}

static volatile LONG g_bridged, g_fallback;
static volatile LONG g_skip_fail;   /* міст не спрацював після пропущеного накопичення FSR 2 */
static int g_san_state(void);
static uint32_t g_san_tot(int i);
/* стан для плагіна AutoFPS: autofps_fsr_status.json поруч з грою */
static void write_status(const char *state)
{
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, path, MAX_PATH);
    while (n > 0 && path[n - 1] != L'\\' && path[n - 1] != L'/') n--;
    path[n] = 0;
    wcscat(path, L"autofps_fsr_status.json");
    FILE *f = _wfopen(path, L"w");
    if (!f) return;
    fprintf(f, "{\"state\":\"%s\",\"version\":\"%s\",\"bridged\":%ld,\"fallback\":%ld,\"known_shaders\":%ld,\"uptime_s\":%lu,"
               "\"sanitize\":%d,\"bad_color\":%u,\"bad_depth\":%u,\"bad_mv\":%u,\"skip_fail\":%ld}\n",
            state, g_ffx_version, g_bridged, g_fallback, g_known_psos, (unsigned long)((GetTickCount() - g_t0) / 1000),
            g_san_state(), g_san_tot(0), g_san_tot(1), g_san_tot(2), g_skip_fail);
    fclose(f);
}

/* статистика для журналу */
static DWORD g_last_stat;
static LARGE_INTEGER g_qpf, g_last_qpc;
static int g_logged_first;

/* ------------------------------------------------------------------ очищення вхідних даних FSR 4
 *
 * Рідний FSR 2 сам гасить погані пікселі (max(0, колір)), FSR 4 — ні: один NaN/Inf у кольорі
 * (сонце, яскраве небо) отруює автоекспозицію всього кадру — кадр блимає чорним — або дає чорну
 * пляму. Перед FFX копіюємо колір, глибину й вектори у власні текстури, замінюючи NaN/Inf
 * (шейдер sanitize.hlsl, DXIL). Заразом глибина йде у FFX як R32_FLOAT (а не D32S8, для якого
 * перегляд R32_FLOAT невалідний). Лічильники поганих пікселів — у журнал і статус.
 */
#if defined(__has_include)
#if __has_include("sanitize_cs.h")
#include "sanitize_cs.h"
#define HAVE_SANITIZE 1
#endif
#endif
#ifndef HAVE_SANITIZE
#define HAVE_SANITIZE 0
#endif

#define SAN_RING 64     /* наборів дескрипторів (кадрів «у польоті» значно менше) */
#define SAN_DESC 7      /* 3 SRV + 4 UAV */
#define SAN_RB 8        /* слотів зчитування лічильників */
#define SAN_EVERY 30    /* копіювати лічильники раз на стільки кадрів */
#define SAN_GRAVE 8

static struct {
    int state;                          /* 0 — ще не пробували, 1 — готово, -1 — недоступно */
    ID3D12RootSignature *rs;
    ID3D12PipelineState *pso;
    ID3D12DescriptorHeap *heap;
    D3D12_CPU_DESCRIPTOR_HANDLE cpu0;
    D3D12_GPU_DESCRIPTOR_HANDLE gpu0;
    UINT inc, slot;
    ID3D12Resource *color, *depth, *mv;
    uint32_t w, h;
    int in_srv;                         /* текстури зараз у стані читання (після FFX) */
    ID3D12Resource *counters, *readback;
    const volatile uint32_t *rb;
    uint32_t rb_slot, copies, frames;
    uint32_t base[4], seen_base;        /* перші прочитані значення (лічильники накопичувальні) */
    uint32_t tot[4];                    /* усього з початку гри: колір, глибина, вектори, кадри */
    uint32_t logged[3];
    DWORD last_log;
    ID3D12Resource *grave[SAN_GRAVE];   /* старі текстури: звільняємо, коли GPU точно їх відпрацював */
    uint32_t grave_frame[SAN_GRAVE];
} g_san;

static void san_log_counters(void)
{
    if (!g_san.rb || g_san.copies < SAN_RB) return;
    uint32_t s = (g_san.rb_slot + 1) % SAN_RB;      /* найстаріший слот — GPU вже давно його записав */
    uint32_t v[4];
    for (int i = 0; i < 4; i++) v[i] = g_san.rb[s * 64 + i];
    if (!g_san.seen_base) { memcpy(g_san.base, v, sizeof(v)); g_san.seen_base = 1; return; }
    for (int i = 0; i < 4; i++) g_san.tot[i] = v[i] - g_san.base[i];
    DWORD t = GetTickCount();
    int fresh = g_san.tot[0] != g_san.logged[0] || g_san.tot[1] != g_san.logged[1] || g_san.tot[2] != g_san.logged[2];
    if (fresh && t - g_san.last_log > 5000) {
        LOG("очищення: виправлено пікселів — колір (NaN/Inf) +%u, глибина +%u, вектори руху +%u; усього %u/%u/%u за %u кадрів",
            g_san.tot[0] - g_san.logged[0], g_san.tot[1] - g_san.logged[1], g_san.tot[2] - g_san.logged[2],
            g_san.tot[0], g_san.tot[1], g_san.tot[2], g_san.tot[3]);
        memcpy(g_san.logged, g_san.tot, sizeof(g_san.logged));
        g_san.last_log = t;
    }
}

static ID3D12Resource *san_buffer(ID3D12Device *dev, D3D12_HEAP_TYPE heap, UINT64 size, D3D12_RESOURCE_FLAGS fl, D3D12_RESOURCE_STATES st)
{
    D3D12_HEAP_PROPERTIES hp; memset(&hp, 0, sizeof(hp)); hp.Type = heap;
    D3D12_RESOURCE_DESC rd; memset(&rd, 0, sizeof(rd));
    rd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; rd.Width = size; rd.Height = 1; rd.DepthOrArraySize = 1;
    rd.MipLevels = 1; rd.SampleDesc.Count = 1; rd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR; rd.Flags = fl;
    ID3D12Resource *r = NULL;
    if (FAILED(ID3D12Device_CreateCommittedResource(dev, &hp, D3D12_HEAP_FLAG_NONE, &rd, st, NULL, &IID_ID3D12Resource, (void **)&r))) return NULL;
    return r;
}

static ID3D12Resource *san_tex(ID3D12Device *dev, DXGI_FORMAT f, uint32_t w, uint32_t h)
{
    D3D12_HEAP_PROPERTIES hp; memset(&hp, 0, sizeof(hp)); hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd; memset(&rd, 0, sizeof(rd));
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; rd.Width = w; rd.Height = h; rd.DepthOrArraySize = 1;
    rd.MipLevels = 1; rd.Format = f; rd.SampleDesc.Count = 1; rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    ID3D12Resource *r = NULL;
    if (FAILED(ID3D12Device_CreateCommittedResource(dev, &hp, D3D12_HEAP_FLAG_NONE, &rd, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                                    NULL, &IID_ID3D12Resource, (void **)&r))) return NULL;
    return r;
}

static int san_init(ID3D12Device *dev)
{
#if HAVE_SANITIZE
    if (g_san.state) return g_san.state > 0;
    g_san.state = -1;
    if (FAILED(o_CreateRootSignature(dev, 0, g_sanitize_cs, sizeof(g_sanitize_cs), &IID_ID3D12RootSignature, (void **)&g_san.rs))) {
        LOG("очищення: не вдалося створити root signature — вимкнено"); return 0;
    }
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd; memset(&pd, 0, sizeof(pd));
    pd.pRootSignature = g_san.rs;
    pd.CS.pShaderBytecode = g_sanitize_cs; pd.CS.BytecodeLength = sizeof(g_sanitize_cs);
    if (FAILED(o_CreateComputePSO(dev, &pd, &IID_ID3D12PipelineState, (void **)&g_san.pso))) {
        LOG("очищення: не вдалося створити PSO — вимкнено"); return 0;
    }
    D3D12_DESCRIPTOR_HEAP_DESC hd; memset(&hd, 0, sizeof(hd));
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV; hd.NumDescriptors = SAN_RING * SAN_DESC;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    if (FAILED(o_CreateDescriptorHeap(dev, &hd, &IID_ID3D12DescriptorHeap, (void **)&g_san.heap))) {
        LOG("очищення: немає купи дескрипторів — вимкнено"); return 0;
    }
    ID3D12DescriptorHeap_GetCPUDescriptorHandleForHeapStart(g_san.heap, &g_san.cpu0);
    ID3D12DescriptorHeap_GetGPUDescriptorHandleForHeapStart(g_san.heap, &g_san.gpu0);
    g_san.inc = ID3D12Device_GetDescriptorHandleIncrementSize(dev, D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    g_san.counters = san_buffer(dev, D3D12_HEAP_TYPE_DEFAULT, 256, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS,
                                D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    g_san.readback = san_buffer(dev, D3D12_HEAP_TYPE_READBACK, 256 * SAN_RB, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_COPY_DEST);
    if (!g_san.counters) { LOG("очищення: немає буфера лічильників — вимкнено"); return 0; }
    if (g_san.readback) {
        void *p = NULL;
        if (SUCCEEDED(o_Map ? o_Map(g_san.readback, 0, NULL, &p) : ID3D12Resource_Map(g_san.readback, 0, NULL, &p)))
            g_san.rb = (const volatile uint32_t *)p;
    }
    g_san.state = 1;
    LOG("очищення вхідних даних FSR 4: увімкнено (NaN/Inf у кольорі, глибина, вектори руху)");
    return 1;
#else
    (void)dev;
    if (!g_san.state) { g_san.state = -1; LOG("очищення: у цій збірці немає шейдера — вимкнено"); }
    return 0;
#endif
}

static DXGI_FORMAT san_srv_fmt(DXGI_FORMAT f, int depth)
{
    switch (f) {
    case DXGI_FORMAT_R32G8X24_TYPELESS: case DXGI_FORMAT_D32_FLOAT_S8X24_UINT: return DXGI_FORMAT_R32_FLOAT_X8X24_TYPELESS;
    case DXGI_FORMAT_R32_TYPELESS: case DXGI_FORMAT_D32_FLOAT: return DXGI_FORMAT_R32_FLOAT;
    case DXGI_FORMAT_R24G8_TYPELESS: case DXGI_FORMAT_D24_UNORM_S8_UINT: return DXGI_FORMAT_R24_UNORM_X8_TYPELESS;
    case DXGI_FORMAT_R16_TYPELESS: case DXGI_FORMAT_D16_UNORM: return DXGI_FORMAT_R16_UNORM;
    case DXGI_FORMAT_R16G16B16A16_TYPELESS: return DXGI_FORMAT_R16G16B16A16_FLOAT;
    case DXGI_FORMAT_R8G8B8A8_TYPELESS: return DXGI_FORMAT_R8G8B8A8_UNORM;
    case DXGI_FORMAT_R10G10B10A2_TYPELESS: return DXGI_FORMAT_R10G10B10A2_UNORM;
    case DXGI_FORMAT_R16G16_TYPELESS: return DXGI_FORMAT_R16G16_FLOAT;
    case DXGI_FORMAT_R32G32_TYPELESS: return DXGI_FORMAT_R32G32_FLOAT;
    case DXGI_FORMAT_R32G32B32A32_TYPELESS: return DXGI_FORMAT_R32G32B32A32_FLOAT;
    default: (void)depth; return f;
    }
}

static void san_barrier3(ID3D12GraphicsCommandList *cl, D3D12_RESOURCE_STATES from, D3D12_RESOURCE_STATES to)
{
    D3D12_RESOURCE_BARRIER b[3]; memset(b, 0, sizeof(b));
    ID3D12Resource *r[3] = {g_san.color, g_san.depth, g_san.mv};
    for (int i = 0; i < 3; i++) {
        b[i].Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        b[i].Transition.pResource = r[i];
        b[i].Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        b[i].Transition.StateBefore = from; b[i].Transition.StateAfter = to;
    }
    ID3D12GraphicsCommandList_ResourceBarrier(cl, 3, b);
}

static void san_buf_barrier(ID3D12GraphicsCommandList *cl, ID3D12Resource *r, D3D12_RESOURCE_STATES from, D3D12_RESOURCE_STATES to)
{
    D3D12_RESOURCE_BARRIER b; memset(&b, 0, sizeof(b));
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = r; b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = from; b.Transition.StateAfter = to;
    ID3D12GraphicsCommandList_ResourceBarrier(cl, 1, &b);
}

/* записує очищення в командний список; повертає 1 і власні текстури, або 0 (тоді FFX бере оригінали) */
static int san_run(ID3D12GraphicsCommandList *cl, ID3D12Device *dev, ID3D12Resource *color, ID3D12Resource *depth,
                   ID3D12Resource *mv, ID3D12Resource **oc, ID3D12Resource **od, ID3D12Resource **om)
{
    if (!g_cfg.sanitize || !san_init(dev)) return 0;
    D3D12_RESOURCE_DESC dc, dz, dm;
    ID3D12Resource_GetDesc(color, &dc); ID3D12Resource_GetDesc(depth, &dz); ID3D12Resource_GetDesc(mv, &dm);
    uint32_t w = (uint32_t)dc.Width, h = dc.Height;
    if (dz.Width != w || dz.Height != h || dm.Width != w || dm.Height != h || w > 16384 || h > 16384) return 0;

    g_san.frames++;
    for (int i = 0; i < SAN_GRAVE; i++)           /* текстури старого розміру — через ~4 с (240 кадрів) */
        if (g_san.grave[i] && g_san.frames - g_san.grave_frame[i] > 240) { ID3D12Resource_Release(g_san.grave[i]); g_san.grave[i] = NULL; }
    if (!g_san.color || g_san.w != w || g_san.h != h) {
        ID3D12Resource *old[3] = {g_san.color, g_san.depth, g_san.mv};
        for (int k = 0; k < 3; k++) if (old[k]) {
            int put = 0;
            for (int i = 0; i < SAN_GRAVE && !put; i++) if (!g_san.grave[i]) { g_san.grave[i] = old[k]; g_san.grave_frame[i] = g_san.frames; put = 1; }
            if (!put) ID3D12Resource_Release(old[k]);   /* не мало б статися: розмір змінюється рідко */
        }
        g_san.color = san_tex(dev, DXGI_FORMAT_R16G16B16A16_FLOAT, w, h);
        g_san.depth = san_tex(dev, DXGI_FORMAT_R32_FLOAT, w, h);
        g_san.mv = san_tex(dev, DXGI_FORMAT_R16G16_FLOAT, w, h);
        g_san.w = w; g_san.h = h; g_san.in_srv = 0;
        if (!g_san.color || !g_san.depth || !g_san.mv) { LOG("очищення: не вдалося створити текстури %ux%u — вимкнено", w, h); g_san.state = -1; return 0; }
        LOG("очищення: текстури %ux%u", w, h);
    }

    UINT slot = (g_san.slot++) % SAN_RING;
    D3D12_CPU_DESCRIPTOR_HANDLE c = g_san.cpu0; c.ptr += (SIZE_T)slot * SAN_DESC * g_san.inc;
    D3D12_GPU_DESCRIPTOR_HANDLE g = g_san.gpu0; g.ptr += (UINT64)slot * SAN_DESC * g_san.inc;
    ID3D12Resource *src[3] = {color, depth, mv};
    DXGI_FORMAT sf[3] = {dc.Format, dz.Format, dm.Format};
    for (int i = 0; i < 3; i++) {
        D3D12_SHADER_RESOURCE_VIEW_DESC sd; memset(&sd, 0, sizeof(sd));
        sd.Format = san_srv_fmt(sf[i], i == 1);
        sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        sd.Texture2D.MipLevels = 1;
        D3D12_CPU_DESCRIPTOR_HANDLE h1 = c; h1.ptr += (SIZE_T)i * g_san.inc;
        o_CreateSRV(dev, src[i], &sd, h1);
    }
    ID3D12Resource *dst[3] = {g_san.color, g_san.depth, g_san.mv};
    DXGI_FORMAT df[3] = {DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_R32_FLOAT, DXGI_FORMAT_R16G16_FLOAT};
    for (int i = 0; i < 3; i++) {
        D3D12_UNORDERED_ACCESS_VIEW_DESC ud; memset(&ud, 0, sizeof(ud));
        ud.Format = df[i]; ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
        D3D12_CPU_DESCRIPTOR_HANDLE h1 = c; h1.ptr += (SIZE_T)(3 + i) * g_san.inc;
        o_CreateUAV(dev, dst[i], NULL, &ud, h1);
    }
    {
        D3D12_UNORDERED_ACCESS_VIEW_DESC ud; memset(&ud, 0, sizeof(ud));
        ud.Format = DXGI_FORMAT_R32_TYPELESS; ud.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
        ud.Buffer.NumElements = 64; ud.Buffer.Flags = D3D12_BUFFER_UAV_FLAG_RAW;
        D3D12_CPU_DESCRIPTOR_HANDLE h1 = c; h1.ptr += (SIZE_T)6 * g_san.inc;
        o_CreateUAV(dev, g_san.counters, NULL, &ud, h1);
    }

    if (g_san.in_srv) san_barrier3(cl, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    ID3D12DescriptorHeap *hp[1] = {g_san.heap};
    o_SetDescriptorHeaps(cl, 1, hp);
    o_SetComputeRootSignature(cl, g_san.rs);
    o_SetPipelineState(cl, g_san.pso);
    o_SetComputeRootDescriptorTable(cl, 0, g);
    struct { uint32_t w, h; float maxc, maxmv; } k = {w, h, 65000.0f, 16384.0f};
    o_SetComputeRoot32BitConstants(cl, 1, 4, &k, 0);
    o_Dispatch(cl, (w + 7) / 8, (h + 7) / 8, 1);
    san_barrier3(cl, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    g_san.in_srv = 1;

    if (g_san.readback && g_san.frames % SAN_EVERY == 0) {
        g_san.rb_slot = (g_san.rb_slot + 1) % SAN_RB;
        san_buf_barrier(cl, g_san.counters, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        ID3D12GraphicsCommandList_CopyBufferRegion(cl, g_san.readback, (UINT64)g_san.rb_slot * 256, g_san.counters, 0, 16);
        san_buf_barrier(cl, g_san.counters, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        g_san.copies++;
        san_log_counters();
    }
    *oc = g_san.color; *od = g_san.depth; *om = g_san.mv;
    return 1;
}

static int g_san_state(void) { return g_san.state; }
static uint32_t g_san_tot(int i) { return g_san.tot[i]; }


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
    AcquireSRWLockExclusive(&g_fr_lock); fr = *fr_get(cl); ReleaseSRWLockExclusive(&g_fr_lock);
    ID3D12Resource *out = table_res(st, 2, 0, D_UAV);
    if (fr.have_cb) {   /* вихід — UAV розміру екрана; у RE4 це u0 таблиці 2, в інших іграх шукаємо */
        D3D12_RESOURCE_DESC dout;
        if (out) ID3D12Resource_GetDesc(out, &dout);
        if (!out || dout.Width != fr.cb[2] || dout.Height != fr.cb[3]) out = find_res(st, D_UAV, NULL, 0, fr.cb[2], fr.cb[3]);
    }
    ID3D12Resource *depth = g_cfg.depth_conv ? fr.depth_conv : fr.depth;
    const char *why = NULL;
    if (fr.cl != cl) why = "інший командний список";
    else if (!fr.color) why = "немає кольору";
    else if (!depth) why = "немає глибини";
    else {
        D3D12_RESOURCE_DESC dc, dz;
        ID3D12Resource_GetDesc(fr.color, &dc); ID3D12Resource_GetDesc(depth, &dz);
        if (dc.Width != dz.Width || dc.Height != dz.Height) why = "глибина іншого розміру";
    }
    if (why) {}
    else if (!fr.mv) why = "немає векторів руху";
    else if (!fr.have_cb) why = "немає констант FSR 2";
    else if (!out) why = "немає виходу";
    if (why) {
        InterlockedIncrement(&g_fallback);
        int wi = !fr.color ? 1 : !depth ? 2 : !fr.mv ? 3 : !fr.have_cb ? 4 : !out ? 5 : 0;
        if (InterlockedIncrement(&g_why[wi]) <= 3) LOG("міст: пропуск кадру (%s) — рідний FSR 2", why);
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
    if (!ok) { ID3D12Device_Release(dev); ReleaseSRWLockExclusive(&g_ffx_lock); return 0; }
    /* очищення входів (NaN/Inf) — у той самий командний список, перед FFX */
    ID3D12Resource *in_color = fr.color, *in_depth = depth, *in_mv = fr.mv;
    t_in_ffx = 1;
    int sanitized = san_run(cl, dev, fr.color, depth, fr.mv, &in_color, &in_depth, &in_mv);
    t_in_ffx = 0;
    ID3D12Device_Release(dev);

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
    dd.color = ffx_res(in_color, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    dd.depth = ffx_res(in_depth, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    dd.motionVectors = ffx_res(in_mv, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    if (g_cfg.exposure && fr.exposure) dd.exposure = ffx_res(fr.exposure, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    if (g_cfg.reactive && fr.reactive) dd.reactive = ffx_res(fr.reactive, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    dd.output = ffx_res(out, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
    dd.jitterOffset.x = f[10] * g_cfg.jx;
    dd.jitterOffset.y = f[11] * g_cfg.jy;
    /* mv_x/mv_y — множник до renderSize: 1 — вектори в UV [0..1], 1/renderSize — у пікселях */
    dd.motionVectorScale.x = g_cfg.mvx * (float)rw;
    dd.motionVectorScale.y = g_cfg.mvy * (float)rh;
    dd.renderSize.width = rw; dd.renderSize.height = rh;
    dd.upscaleSize.width = dw; dd.upscaleSize.height = dh;
    dd.enableSharpening = g_cfg.sharpness > 0.0f;
    dd.sharpness = g_cfg.sharpness;
    dd.frameTimeDelta = dt_ms;
    dd.preExposure = f[28] > 0.0f ? f[28] : 1.0f;
    static uint32_t last_frame_index;
    dd.reset = fr.cb[7] + 8 < last_frame_index;   /* лічильник кадрів FSR 2 скинувся — монтажна склейка */
    if (InterlockedExchange(&g_cfg_changed, 0)) dd.reset = 1;
    last_frame_index = fr.cb[7];
    /* FFX: при оберненій глибині near > far; для нескінченної оберненої near = FLT_MAX */
    if (g_cfg.flags & FFX_UPSCALE_ENABLE_DEPTH_INVERTED) {
        dd.cameraFar = g_cfg.cam_near;
        dd.cameraNear = (g_cfg.flags & FFX_UPSCALE_ENABLE_DEPTH_INFINITE) ? 3.402823466e+38f : g_cfg.cam_far;
    } else {
        dd.cameraNear = g_cfg.cam_near;
        dd.cameraFar = (g_cfg.flags & FFX_UPSCALE_ENABLE_DEPTH_INFINITE) ? 3.402823466e+38f : g_cfg.cam_far;
    }
    dd.cameraFovAngleVertical = (f[29] > 0.01f && f[29] < 10.f) ? 2.0f * atanf(f[29]) : 1.0f;
    dd.viewSpaceToMetersFactor = 1.0f;

    t_in_ffx = 1;
    ffxReturnCode_t rc = f_dispatch(&g_ctx, &dd.header);
    t_in_ffx = 0;
    ReleaseSRWLockExclusive(&g_ffx_lock);
    restore_state(cl, st);
    (void)sanitized;

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
        LOG("міст: перший кадр через %s: %ux%u→%ux%u jitter=(%.3f,%.3f) mvScale=(%.3f,%.3f) preExp=%.3f fov=%.1f° очищення=%s експозиція=%s",
            g_ffx_version, rw, rh, dw, dh, dd.jitterOffset.x, dd.jitterOffset.y, dd.motionVectorScale.x,
            dd.motionVectorScale.y, dd.preExposure, dd.cameraFovAngleVertical * 57.2958f, sanitized ? "так" : "ні",
            g_cfg.exposure ? (fr.exposure ? "з FSR 2 гри" : "гри не знайдено — 1.0") : "авто FFX");
        write_status("ok");
    }
    DWORD t = GetTickCount();
    if (t - g_last_stat > 30000) {
        g_last_stat = t;
        LOG("міст: кадрів через FFX %ld, з рідним FSR 2 %ld (колір %ld, глибина %ld, вектори %ld, константи %ld, вихід %ld)",
            g_bridged, g_fallback, g_why[1], g_why[2], g_why[3], g_why[4], g_why[5]);
        write_status("ok");
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

/* визначає, яким кроком FSR 2 є невідомий шейдер — за даними самого виклику, без хешів конкретної гри */
static int classify(ID3D12GraphicsCommandList *cl, cl_state *st, UINT x, UINT y, uint64_t hash)
{
    uint32_t cb[64];
    int cls = -1;
    if (fsr2_cb_find(st, x, y, cb)) cls = PC_ACCUM;
    else {
        int prev = 0; uint32_t dw = 0, dh = 0;
        AcquireSRWLockExclusive(&g_fr_lock);
        frame_state *F = fr_get(cl);
        if (F->have_cb) { prev = 1; dw = F->cb[2]; dh = F->cb[3]; }
        ReleaseSRWLockExclusive(&g_fr_lock);
        uint32_t rw = g_rw, rh = g_rh;
        if (prev && x == (dw + 15) / 16 && y == (dh + 15) / 16 && find_res(st, D_UAV, NULL, 0, dw, dh))
            cls = PC_RCAS;
        else if (rw && x == (rw + 63) / 64 && y == (rh + 63) / 64 && find_res(st, D_SRV, k_color_fmts, 4, rw, rh))
            cls = PC_LUMA;
    }
    pso_mark(st->pso, cls);
    if (cls < 0) return PC_UNKNOWN;
    if (cls == PC_ACCUM) InterlockedIncrement(&g_known_psos);
    LOG("автовизначення FSR 2: шейдер %016llx — %s (групи %ux%u)", (unsigned long long)hash, g_pc_name[cls], x, y);
    return cls;
}

static void STDMETHODCALLTYPE h_Dispatch(ID3D12GraphicsCommandList *cl, UINT x, UINT y, UINT z)
{
    if (t_in_ffx || !g_cfg.enable || g_ffx_state < 0) { o_Dispatch(cl, x, y, z); return; }
    cl_state st;
    AcquireSRWLockShared(&g_cl_lock);
    st = *cl_get(cl);
    ReleaseSRWLockShared(&g_cl_lock);
    pso_ent pe;
    if (!st.pso || !pso_get(st.pso, &pe)) { o_Dispatch(cl, x, y, z); return; }
    int cls = pe.cls;
    if (cls == PC_UNKNOWN) cls = classify(cl, &st, x, y, pe.hash);
    uint32_t rw = g_rw, rh = g_rh;
    switch (cls) {
    case PC_PREP:    /* RE4 крок 1: глибина (t2), маска (u0) — початок кадру FSR 2 */
        AcquireSRWLockExclusive(&g_fr_lock);
        { frame_state *F = fr_get(cl); memset(F, 0, sizeof(*F)); F->cl = cl;
          F->depth = find_depth(&st); F->reactive = table_res(&st, 2, 0, D_UAV); }
        ReleaseSRWLockExclusive(&g_fr_lock);
        break;
    case PC_MV:      /* RE4 крок 2: вектори руху для FSR (u0) */
        AcquireSRWLockExclusive(&g_fr_lock);
        { frame_state *F = fr_get(cl);
          F->mv_cand = table_res(&st, 2, 0, D_UAV);
          if (!F->depth) F->depth = find_depth(&st); }
        ReleaseSRWLockExclusive(&g_fr_lock);
        break;
    case PC_DCONV:   /* RE4 крок 3: перетворена глибина (u0, R32_FLOAT) */
        AcquireSRWLockExclusive(&g_fr_lock);
        { frame_state *F = fr_get(cl);
          F->depth_conv = table_res(&st, 2, 0, D_UAV);
          if (!F->depth) F->depth = find_depth(&st); }
        ReleaseSRWLockExclusive(&g_fr_lock);
        break;
    case PC_LUMA: {  /* піраміда яскравості: читає колір кадру в рендер-роздільності */
        ID3D12Resource *c = rw ? find_res(&st, D_SRV, k_color_fmts, 4, rw, rh) : NULL;
        if (!c) c = table_find(&st, 1, DXGI_FORMAT_R11G11B10_FLOAT, 0, 0, D_SRV);
        ID3D12Resource *d = find_depth_any(&st, rw, rh);
        ID3D12Resource *exp = find_res(&st, D_UAV, k_exp_fmts, 1, 1, 1);
        AcquireSRWLockExclusive(&g_fr_lock);
        frame_state *F = fr_get(cl);
        F->color = c;
        if (F->mv_cand) F->mv = F->mv_cand;   /* вектори — останні перед цим кроком (пізніші — внутрішні FSR 2) */
        if (!F->exposure) F->exposure = exp;  /* експозиція FSR 2 (1x1 R32G32_FLOAT, пише цей крок) */
        if (!F->depth && d) F->depth = d;
        ReleaseSRWLockExclusive(&g_fr_lock);
        break;
    }
    case PC_ACCUM: { /* накопичення — константи FSR 2 */
        uint32_t cb[64];
        int ok = fsr2_cb_find(&st, x, y, cb);
        if (ok) { g_rw = cb[0]; g_rh = cb[1]; g_dw = cb[2]; g_dh = cb[3]; }
        ID3D12Resource *d = ok ? find_depth_any(&st, cb[0], cb[1]) : NULL;
        AcquireSRWLockExclusive(&g_fr_lock);
        frame_state *F = fr_get(cl);
        if (d) {
            D3D12_RESOURCE_DESC dd;
            int bad = 1;
            if (F->depth) { ID3D12Resource_GetDesc(F->depth, &dd); bad = dd.Width != cb[0] || dd.Height != cb[1]; }
            if (bad) F->depth = d;
        }
        if (ok) { memcpy(F->cb, cb, sizeof(cb)); F->have_cb = 1; }
        int skip = g_cfg.skip_accumulate && g_ffx_state == 1 && ok && F->color && F->mv && F->depth;
        if (skip) {   /* ті самі перевірки, що й на кроці RCAS — інакше вийде кадр без зображення */
            D3D12_RESOURCE_DESC dc, dz;
            ID3D12Resource_GetDesc(F->color, &dc); ID3D12Resource_GetDesc(F->depth, &dz);
            if (dc.Width != dz.Width || dc.Height != dz.Height) skip = 0;
        }
        F->accumulate_skipped = skip;
        ReleaseSRWLockExclusive(&g_fr_lock);
        if (skip) return;
        break;
    }
    case PC_RCAS: {  /* RCAS → тут FSR 4 */
        int done = bridge_rcas(cl, &st);
        int skipped;
        AcquireSRWLockExclusive(&g_fr_lock);
        { frame_state *F = fr_get(cl); skipped = F->accumulate_skipped;
          memset(F, 0, sizeof(*F)); F->cl = cl; }   /* кадр FSR 2 завершено */
        ReleaseSRWLockExclusive(&g_fr_lock);
        if (done) return;
        if (skipped) {   /* накопичення FSR 2 пропущено — рідний RCAS дав би чорний кадр; лишаємо попередній */
            LONG n = InterlockedIncrement(&g_skip_fail);
            if (n <= 3) LOG("міст: кадр не передано після пропущеного накопичення FSR 2 (%ld)", n);
            if (n == 3 && g_cfg.skip_accumulate) {
                g_cfg.skip_accumulate = 0;   /* щоб не було «застиглих»/чорних кадрів — FSR 2 рахує накопичення завжди */
                LOG("міст: пропуск накопичення FSR 2 вимкнено до кінця гри (кадри без зображення)");
            }
            return;
        }
        break;
    }
    default:
        /* кандидат у вектори руху: R16G16_FLOAT рендер-розміру, групи 8x8 по рендер-роздільності */
        if (rw && x == (rw + 7) / 8 && y == (rh + 7) / 8) {
            ID3D12Resource *m = find_res(&st, D_UAV, k_mv_fmts, 1, rw, rh);
            AcquireSRWLockExclusive(&g_fr_lock);
            { frame_state *F = fr_get(cl); if (m) F->mv_cand = m; }
            ReleaseSRWLockExclusive(&g_fr_lock);
        }
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
        LOG("AutoFPS FSR2→FSR4 міст, збірка " __DATE__ " " __TIME__);
        load_cfg();
        QueryPerformanceFrequency(&g_qpf);
        { HANDLE w = CreateThread(NULL, 0, cfg_watch_thread, NULL, 0, NULL); if (w) CloseHandle(w); }
        if (!g_pso || !g_desc || !g_rs || !g_maps) { LOG("немає пам'яті для таблиць"); return TRUE; }
        if (MH_Initialize() != MH_OK) { LOG("MH_Initialize помилка"); return TRUE; }
        HANDLE t = CreateThread(NULL, 0, init_thread, NULL, 0, NULL);
        if (t) CloseHandle(t);
    }
    return TRUE;
}
