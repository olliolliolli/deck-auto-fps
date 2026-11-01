/*
 * AutoFPS FSR2→FSR4 bridge — етап 1: ДІАГНОСТИКА.
 *
 * Завантажується грою як dinput8.dll (WINEDLLOVERRIDES=dinput8=n,b), усі виклики DirectInput
 * передає справжній системній dinput8.dll. Нічого не змінює в картинці — лише пише журнал
 * autofps_fsr_diag.log поруч з re4.exe:
 *   - кожен створений compute-шейдер (PSO): хеш байткоду, розмір, рядки з байткоду
 *     (за ними видно кроки FSR 2 рушія і назви їхніх ресурсів);
 *   - протягом кількох секунд гри — послідовність Dispatch (який шейдер, скільки груп),
 *     прив'язки кореневих параметрів і відправки на чергу GPU.
 * З цього журналу видно, де саме рушій виконує FSR 2 і які текстури йому подає.
 *
 * Ліцензія: BSD-3-Clause (як AutoFPS). MinHook — BSD-2-Clause (third_party/minhook).
 */
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <d3d12.h>
#include <stdlib.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "MinHook.h"

/* ------------------------------------------------------------------ журнал */

static FILE *g_log;
static CRITICAL_SECTION g_cs;
static DWORD g_t0;
static volatile LONG g_lines;

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

/* ------------------------------------------------------------------ налаштування вікна заміру */

/* Запис Dispatch вікнами: перше через 60 с після старту, далі щохвилини по 2.5 с, 6 разів —
   щоб хоч одне вікно точно припало на гру з увімкненим FSR 2, а не на меню. */
static DWORD g_win_start_ms = 60000;
static DWORD g_win_len_ms = 2500;
static DWORD g_win_period_ms = 60000;
static DWORD g_win_count = 6;
static LONG g_max_lines = 150000;

static int in_window(void)
{
    DWORD t = GetTickCount() - g_t0;
    if (t < g_win_start_ms || g_lines >= g_max_lines) return 0;
    DWORD k = (t - g_win_start_ms) / g_win_period_ms;
    return k < g_win_count && (t - g_win_start_ms) % g_win_period_ms < g_win_len_ms;
}

/* ------------------------------------------------------------------ таблиці (вказівник → значення) */

typedef struct { void *key; uint32_t id; uint32_t size; uint64_t hash; } pso_ent;
#define PSO_CAP (1u << 18)
static pso_ent *g_pso;
static volatile LONG g_pso_count;
static SRWLOCK g_pso_lock = SRWLOCK_INIT;

typedef struct { void *key; void *val; } ptr_ent;
#define CL_CAP (1u << 14)
static ptr_ent g_cl_pso[CL_CAP];
static ptr_ent g_cl_rs[CL_CAP];
static SRWLOCK g_cl_lock = SRWLOCK_INIT;

static uint32_t ptr_slot(void *p, uint32_t cap)
{
    uint64_t x = (uint64_t)(uintptr_t)p;
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33;
    return (uint32_t)x & (cap - 1);
}

static void pso_put(void *key, uint64_t hash, uint32_t size, uint32_t id)
{
    AcquireSRWLockExclusive(&g_pso_lock);
    uint32_t i = ptr_slot(key, PSO_CAP);
    for (uint32_t n = 0; n < PSO_CAP; n++, i = (i + 1) & (PSO_CAP - 1)) {
        if (g_pso[i].key == NULL || g_pso[i].key == key) {
            g_pso[i].key = key; g_pso[i].hash = hash; g_pso[i].size = size; g_pso[i].id = id;
            break;
        }
    }
    ReleaseSRWLockExclusive(&g_pso_lock);
}

static int pso_get(void *key, pso_ent *out)
{
    int ok = 0;
    AcquireSRWLockShared(&g_pso_lock);
    uint32_t i = ptr_slot(key, PSO_CAP);
    for (uint32_t n = 0; n < 64; n++, i = (i + 1) & (PSO_CAP - 1)) {
        if (g_pso[i].key == key) { *out = g_pso[i]; ok = 1; break; }
        if (g_pso[i].key == NULL) break;
    }
    ReleaseSRWLockShared(&g_pso_lock);
    return ok;
}

static void ptr_put(ptr_ent *t, void *k, void *v)
{
    AcquireSRWLockExclusive(&g_cl_lock);
    uint32_t i = ptr_slot(k, CL_CAP);
    for (uint32_t n = 0; n < CL_CAP; n++, i = (i + 1) & (CL_CAP - 1)) {
        if (t[i].key == NULL || t[i].key == k) { t[i].key = k; t[i].val = v; break; }
    }
    ReleaseSRWLockExclusive(&g_cl_lock);
}

static void *ptr_get(ptr_ent *t, void *k)
{
    void *v = NULL;
    AcquireSRWLockShared(&g_cl_lock);
    uint32_t i = ptr_slot(k, CL_CAP);
    for (uint32_t n = 0; n < 64; n++, i = (i + 1) & (CL_CAP - 1)) {
        if (t[i].key == k) { v = t[i].val; break; }
        if (t[i].key == NULL) break;
    }
    ReleaseSRWLockShared(&g_cl_lock);
    return v;
}

/* ------------------------------------------------------------------ байткод шейдера */

static uint64_t fnv1a(const void *p, size_t n)
{
    const uint8_t *b = (const uint8_t *)p;
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ULL; }
    return h;
}

/* частини контейнера DXBC/DXIL (DXIL, RDAT, PSV0, STAT, ILDN …) + друковані рядки */
static void describe_shader(uint32_t id, const D3D12_SHADER_BYTECODE *bc, const char *how)
{
    const uint8_t *b = (const uint8_t *)bc->pShaderBytecode;
    size_t n = bc->BytecodeLength;
    uint64_t h = fnv1a(b, n);
    char parts[256] = {0};
    if (n >= 32 && memcmp(b, "DXBC", 4) == 0) {
        uint32_t count = *(const uint32_t *)(b + 28);
        for (uint32_t i = 0; i < count && i < 24 && 32 + i * 4 + 4 <= n; i++) {
            uint32_t off = *(const uint32_t *)(b + 32 + i * 4);
            if (off + 8 <= n) {
                char fourcc[6] = {0};
                memcpy(fourcc, b + off, 4);
                strncat(parts, fourcc, sizeof(parts) - strlen(parts) - 2);
                strncat(parts, " ", sizeof(parts) - strlen(parts) - 1);
            }
        }
    }
    LOG("PSO #%u %s hash=%016llx size=%zu parts=[%s]", id, how, (unsigned long long)h, n, parts);

    /* рядки: назви ресурсів/змінних, якщо рушій не прибрав відображення (reflection) */
    char line[2048];
    size_t used = 0;
    int shown = 0;
    line[0] = 0;
    size_t start = 0;
    for (size_t i = 0; i <= n && shown < 48; i++) {
        int pr = (i < n) && b[i] >= 0x20 && b[i] < 0x7f;
        if (!pr) {
            size_t len = i - start;
            if (len >= 5 && len < 96) {
                if (used + len + 3 >= sizeof(line)) {
                    LOG("   str: %s", line);
                    used = 0; line[0] = 0;
                }
                memcpy(line + used, b + start, len);
                used += len;
                line[used++] = ' ';
                line[used++] = '|';
                line[used] = 0;
                shown++;
            }
            start = i + 1;
        }
    }
    if (used) LOG("   str: %s", line);
}

/* ------------------------------------------------------------------ хуки D3D12 */

typedef HRESULT (STDMETHODCALLTYPE *PFN_CreateComputePSO)(ID3D12Device *, const D3D12_COMPUTE_PIPELINE_STATE_DESC *, REFIID, void **);
typedef HRESULT (STDMETHODCALLTYPE *PFN_CreatePSO2)(ID3D12Device2 *, const D3D12_PIPELINE_STATE_STREAM_DESC *, REFIID, void **);
typedef HRESULT (STDMETHODCALLTYPE *PFN_LoadComputePipeline)(ID3D12PipelineLibrary *, LPCWSTR, const D3D12_COMPUTE_PIPELINE_STATE_DESC *, REFIID, void **);
typedef HRESULT (STDMETHODCALLTYPE *PFN_CreateRootSignature)(ID3D12Device *, UINT, const void *, SIZE_T, REFIID, void **);
typedef void (STDMETHODCALLTYPE *PFN_SetPipelineState)(ID3D12GraphicsCommandList *, ID3D12PipelineState *);
typedef void (STDMETHODCALLTYPE *PFN_Dispatch)(ID3D12GraphicsCommandList *, UINT, UINT, UINT);
typedef void (STDMETHODCALLTYPE *PFN_SetComputeRootSignature)(ID3D12GraphicsCommandList *, ID3D12RootSignature *);
typedef void (STDMETHODCALLTYPE *PFN_SetComputeRootDescriptorTable)(ID3D12GraphicsCommandList *, UINT, D3D12_GPU_DESCRIPTOR_HANDLE);
typedef void (STDMETHODCALLTYPE *PFN_SetComputeRoot32BitConstants)(ID3D12GraphicsCommandList *, UINT, UINT, const void *, UINT);
typedef void (STDMETHODCALLTYPE *PFN_SetComputeRootCBV)(ID3D12GraphicsCommandList *, UINT, D3D12_GPU_VIRTUAL_ADDRESS);
typedef void (STDMETHODCALLTYPE *PFN_ExecuteCommandLists)(ID3D12CommandQueue *, UINT, ID3D12CommandList *const *);

static PFN_CreateComputePSO o_CreateComputePSO;
static PFN_CreatePSO2 o_CreatePSO2;
static PFN_LoadComputePipeline o_LoadComputePipeline;
static PFN_CreateRootSignature o_CreateRootSignature;
static PFN_SetPipelineState o_SetPipelineState;
static PFN_Dispatch o_Dispatch;
static PFN_SetComputeRootSignature o_SetComputeRootSignature;
static PFN_SetComputeRootDescriptorTable o_SetComputeRootDescriptorTable;
static PFN_SetComputeRoot32BitConstants o_SetComputeRoot32BitConstants;
static PFN_SetComputeRootCBV o_SetComputeRootCBV;
static PFN_ExecuteCommandLists o_ExecuteCommandLists;

static void register_pso(void *pso, const D3D12_SHADER_BYTECODE *cs, const char *how)
{
    if (!pso || !cs || !cs->pShaderBytecode || !cs->BytecodeLength) return;
    uint32_t id = (uint32_t)InterlockedIncrement(&g_pso_count);
    uint64_t h = fnv1a(cs->pShaderBytecode, cs->BytecodeLength);
    pso_put(pso, h, (uint32_t)cs->BytecodeLength, id);
    describe_shader(id, cs, how);
}

static HRESULT STDMETHODCALLTYPE h_CreateComputePSO(ID3D12Device *dev, const D3D12_COMPUTE_PIPELINE_STATE_DESC *d, REFIID riid, void **out)
{
    HRESULT hr = o_CreateComputePSO(dev, d, riid, out);
    if (SUCCEEDED(hr) && out && *out && d) register_pso(*out, &d->CS, "compute");
    return hr;
}

static HRESULT STDMETHODCALLTYPE h_LoadComputePipeline(ID3D12PipelineLibrary *lib, LPCWSTR name, const D3D12_COMPUTE_PIPELINE_STATE_DESC *d, REFIID riid, void **out)
{
    HRESULT hr = o_LoadComputePipeline(lib, name, d, riid, out);
    if (SUCCEEDED(hr) && out && *out && d) register_pso(*out, &d->CS, "library");
    return hr;
}

/* потік підоб'єктів PSO: шукаємо CS; розміри — за типом, вирівнювання на sizeof(void*) */
static size_t subobj_payload(int type)
{
    switch (type) {
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_ROOT_SIGNATURE: return sizeof(void *);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_VS: case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_PS:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DS: case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_HS:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_GS: case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_CS:
        return sizeof(D3D12_SHADER_BYTECODE);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_STREAM_OUTPUT: return sizeof(D3D12_STREAM_OUTPUT_DESC);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_BLEND: return sizeof(D3D12_BLEND_DESC);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_SAMPLE_MASK: return sizeof(UINT);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_RASTERIZER: return sizeof(D3D12_RASTERIZER_DESC);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL: return sizeof(D3D12_DEPTH_STENCIL_DESC);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_INPUT_LAYOUT: return sizeof(D3D12_INPUT_LAYOUT_DESC);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_IB_STRIP_CUT_VALUE: return sizeof(UINT);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_PRIMITIVE_TOPOLOGY: return sizeof(UINT);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_RENDER_TARGET_FORMATS: return sizeof(D3D12_RT_FORMAT_ARRAY);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL_FORMAT: return sizeof(UINT);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_SAMPLE_DESC: return sizeof(DXGI_SAMPLE_DESC);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_NODE_MASK: return sizeof(UINT);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_CACHED_PSO: return sizeof(D3D12_CACHED_PIPELINE_STATE);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_FLAGS: return sizeof(UINT);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL1: return sizeof(D3D12_DEPTH_STENCIL_DESC1);
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_VIEW_INSTANCING: return sizeof(D3D12_VIEW_INSTANCING_DESC);
    default: return 0;
    }
}

static int payload_align(int type)
{
    switch (type) {
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_ROOT_SIGNATURE:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_VS: case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_PS:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DS: case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_HS:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_GS: case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_CS:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_STREAM_OUTPUT:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_INPUT_LAYOUT:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_CACHED_PSO:
    case D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_VIEW_INSTANCING:
        return 8;
    default:
        return 4;
    }
}

static HRESULT STDMETHODCALLTYPE h_CreatePSO2(ID3D12Device2 *dev, const D3D12_PIPELINE_STATE_STREAM_DESC *d, REFIID riid, void **out)
{
    HRESULT hr = o_CreatePSO2(dev, d, riid, out);
    if (FAILED(hr) || !out || !*out || !d || !d->pPipelineStateSubobjectStream) return hr;
    const uint8_t *p = (const uint8_t *)d->pPipelineStateSubobjectStream;
    size_t off = 0, end = d->SizeInBytes;
    while (off + 4 <= end) {
        int type = *(const int *)(p + off);
        size_t payload = subobj_payload(type);
        if (!payload) break;                                /* невідомий тип — далі не розбираємо */
        size_t a = (size_t)payload_align(type);
        size_t inner = (4 + a - 1) & ~(a - 1);
        if (type == D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_CS && off + inner + sizeof(D3D12_SHADER_BYTECODE) <= end) {
            register_pso(*out, (const D3D12_SHADER_BYTECODE *)(p + off + inner), "stream");
            break;
        }
        off += (inner + payload + sizeof(void *) - 1) & ~(sizeof(void *) - 1);
    }
    return hr;
}

static HRESULT STDMETHODCALLTYPE h_CreateRootSignature(ID3D12Device *dev, UINT mask, const void *blob, SIZE_T len, REFIID riid, void **out)
{
    HRESULT hr = o_CreateRootSignature(dev, mask, blob, len, riid, out);
    if (SUCCEEDED(hr) && out && *out && blob && len >= 4) {
        LOG("RS %p size=%zu hash=%016llx", *out, (size_t)len, (unsigned long long)fnv1a(blob, len));
    }
    return hr;
}

static void STDMETHODCALLTYPE h_SetPipelineState(ID3D12GraphicsCommandList *cl, ID3D12PipelineState *pso)
{
    ptr_put(g_cl_pso, cl, pso);
    o_SetPipelineState(cl, pso);
}

static void STDMETHODCALLTYPE h_SetComputeRootSignature(ID3D12GraphicsCommandList *cl, ID3D12RootSignature *rs)
{
    ptr_put(g_cl_rs, cl, rs);
    if (in_window()) { InterlockedIncrement(&g_lines); LOG("  CL %p setRS %p", cl, rs); }
    o_SetComputeRootSignature(cl, rs);
}

static void STDMETHODCALLTYPE h_SetComputeRootDescriptorTable(ID3D12GraphicsCommandList *cl, UINT idx, D3D12_GPU_DESCRIPTOR_HANDLE h)
{
    if (in_window()) { InterlockedIncrement(&g_lines); LOG("  CL %p table[%u]=%llx", cl, idx, (unsigned long long)h.ptr); }
    o_SetComputeRootDescriptorTable(cl, idx, h);
}

static void STDMETHODCALLTYPE h_SetComputeRoot32BitConstants(ID3D12GraphicsCommandList *cl, UINT idx, UINT n, const void *data, UINT off)
{
    if (in_window()) {
        InterlockedIncrement(&g_lines);
        char buf[512]; size_t u = 0; buf[0] = 0;
        const uint32_t *v = (const uint32_t *)data;
        for (UINT i = 0; i < n && i < 24 && u < sizeof(buf) - 24; i++)
            u += (size_t)snprintf(buf + u, sizeof(buf) - u, "%08x ", v[i]);
        LOG("  CL %p const[%u]+%u n=%u: %s", cl, idx, off, n, buf);
    }
    o_SetComputeRoot32BitConstants(cl, idx, n, data, off);
}

static void STDMETHODCALLTYPE h_SetComputeRootCBV(ID3D12GraphicsCommandList *cl, UINT idx, D3D12_GPU_VIRTUAL_ADDRESS va)
{
    if (in_window()) { InterlockedIncrement(&g_lines); LOG("  CL %p cbv[%u]=%llx", cl, idx, (unsigned long long)va); }
    o_SetComputeRootCBV(cl, idx, va);
}

static void STDMETHODCALLTYPE h_Dispatch(ID3D12GraphicsCommandList *cl, UINT x, UINT y, UINT z)
{
    if (in_window()) {
        InterlockedIncrement(&g_lines);
        void *pso = ptr_get(g_cl_pso, cl);
        pso_ent e;
        if (pso && pso_get(pso, &e))
            LOG("DISPATCH CL %p PSO #%u hash=%016llx groups=%u,%u,%u", cl, e.id, (unsigned long long)e.hash, x, y, z);
        else
            LOG("DISPATCH CL %p PSO %p (невідомий) groups=%u,%u,%u", cl, pso, x, y, z);
    }
    o_Dispatch(cl, x, y, z);
}

static void STDMETHODCALLTYPE h_ExecuteCommandLists(ID3D12CommandQueue *q, UINT n, ID3D12CommandList *const *lists)
{
    if (in_window()) { InterlockedIncrement(&g_lines); LOG("EXECUTE queue %p lists=%u first=%p", q, n, n ? (void *)lists[0] : NULL); }
    o_ExecuteCommandLists(q, n, lists);
}

static int hook(void *target, void *detour, void **orig, const char *name)
{
    if (!target) { LOG("hook %s: немає адреси", name); return 0; }
    MH_STATUS s = MH_CreateHook(target, detour, orig);
    if (s == MH_ERROR_ALREADY_CREATED) return 1;   /* спільна функція в кількох vtable */
    if (s != MH_OK) { LOG("hook %s: MH_CreateHook=%d", name, (int)s); return 0; }
    LOG("hook %s @ %p", name, target);
    return 1;
}

typedef HRESULT (WINAPI *PFN_D3D12CreateDevice_)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);

static DWORD WINAPI init_thread(void *arg)
{
    (void)arg;
    Sleep(200);
    HMODULE d3d12 = LoadLibraryW(L"d3d12.dll");
    PFN_D3D12CreateDevice_ create = d3d12 ? (PFN_D3D12CreateDevice_)GetProcAddress(d3d12, "D3D12CreateDevice") : NULL;
    if (!create) { LOG("d3d12.dll / D3D12CreateDevice не знайдено"); return 0; }

    /* власний тимчасовий пристрій: у vkd3d-proton усі об'єкти одного типу мають спільні
       функції в vtable, тож хуки на них діють і на пристрій гри (навіть уже створений) */
    ID3D12Device *dev = NULL;
    HRESULT hr = create(NULL, D3D_FEATURE_LEVEL_11_0, &IID_ID3D12Device, (void **)&dev);
    if (FAILED(hr) || !dev) { LOG("D3D12CreateDevice (тимчасовий) = %08lx", (unsigned long)hr); return 0; }

    ID3D12CommandAllocator *alloc = NULL;
    ID3D12GraphicsCommandList *cl = NULL;
    ID3D12CommandQueue *queue = NULL;
    D3D12_COMMAND_QUEUE_DESC qd = {0};
    qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    ID3D12Device_CreateCommandQueue(dev, &qd, &IID_ID3D12CommandQueue, (void **)&queue);
    ID3D12Device_CreateCommandAllocator(dev, D3D12_COMMAND_LIST_TYPE_DIRECT, &IID_ID3D12CommandAllocator, (void **)&alloc);
    if (alloc)
        ID3D12Device_CreateCommandList(dev, 0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, NULL, &IID_ID3D12GraphicsCommandList, (void **)&cl);

    hook((void *)dev->lpVtbl->CreateComputePipelineState, (void *)h_CreateComputePSO, (void **)&o_CreateComputePSO, "CreateComputePipelineState");
    hook((void *)dev->lpVtbl->CreateRootSignature, (void *)h_CreateRootSignature, (void **)&o_CreateRootSignature, "CreateRootSignature");

    ID3D12Device2 *dev2 = NULL;
    if (SUCCEEDED(ID3D12Device_QueryInterface(dev, &IID_ID3D12Device2, (void **)&dev2)) && dev2) {
        hook((void *)dev2->lpVtbl->CreatePipelineState, (void *)h_CreatePSO2, (void **)&o_CreatePSO2, "CreatePipelineState(stream)");
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
    if (queue)
        hook((void *)queue->lpVtbl->ExecuteCommandLists, (void *)h_ExecuteCommandLists, (void **)&o_ExecuteCommandLists, "ExecuteCommandLists");

    MH_STATUS s = MH_EnableHook(MH_ALL_HOOKS);
    LOG("MH_EnableHook = %d; запис Dispatch: з %lu мс, кожні %lu мс по %lu мс, %lu разів", (int)s,
        (unsigned long)g_win_start_ms, (unsigned long)g_win_period_ms, (unsigned long)g_win_len_ms,
        (unsigned long)g_win_count);

    if (cl) ID3D12GraphicsCommandList_Release(cl);
    if (alloc) ID3D12CommandAllocator_Release(alloc);
    if (queue) ID3D12CommandQueue_Release(queue);
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
        if (!g_real) LOG("не вдалося завантажити системну dinput8.dll");
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

static DWORD env_ms(const char *name, DWORD def)
{
    char buf[32];
    DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
    return (n > 0 && n < sizeof(buf)) ? (DWORD)strtoul(buf, NULL, 10) : def;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_cs);
        g_t0 = GetTickCount();
        g_win_start_ms = env_ms("AUTOFPS_DIAG_START_MS", g_win_start_ms);
        g_win_len_ms = env_ms("AUTOFPS_DIAG_LEN_MS", g_win_len_ms);
        g_pso = (pso_ent *)VirtualAlloc(NULL, sizeof(pso_ent) * PSO_CAP, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        open_log();
        LOG("AutoFPS FSR2-міст: діагностика (етап 1), збірка " __DATE__ " " __TIME__);
        if (MH_Initialize() != MH_OK) { LOG("MH_Initialize помилка"); return TRUE; }
        HANDLE t = CreateThread(NULL, 0, init_thread, NULL, 0, NULL);
        if (t) CloseHandle(t);
    }
    return TRUE;
}
