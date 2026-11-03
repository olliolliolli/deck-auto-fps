// AutoFPS FSR2→FSR4 міст: «ремонт» виходу FSR 4 — останній рубіж проти чорних спалахів.
//
// Після FSR 4 перевіряємо кожен піксель результату. Якщо він NaN/Inf (нейромережа «вибухнула»)
// або майже чорний там, де вхідний кадр гри яскравий (зіпсована експозиція/історія), — ставимо
// замість нього просте збільшення вхідного кадру (білінійно). Справжні темні деталі не чіпаємо:
// заміна лише коли результат темніший за вхід у десятки разів.
#define RS "DescriptorTable(SRV(t0, numDescriptors = 1), UAV(u0, numDescriptors = 2)), " \
           "RootConstants(num32BitConstants = 12, b0), " \
           "StaticSampler(s0, filter = FILTER_MIN_MAG_MIP_LINEAR, addressU = TEXTURE_ADDRESS_CLAMP, " \
           "addressV = TEXTURE_ADDRESS_CLAMP, addressW = TEXTURE_ADDRESS_CLAMP)"

Texture2D<float4>   InColor  : register(t0);   // очищений вхідний кадр (рендер-роздільність)
RWTexture2D<float4> Out      : register(u0);   // вихід FSR 4 (екранна роздільність), на місці
RWByteAddressBuffer Counters : register(u1);
SamplerState        Lin      : register(s0);

cbuffer Params : register(b0)
{
    uint2  OutSize;     // розмір виходу
    float2 UvScale;     // renderSize / розмір вхідної текстури (рендер займає лівий верхній кут)
    float  DarkRatio;   // «чорний»: яскравість виходу < DarkRatio × яскравість входу
    float  MinLuma;     // і вхід яскравіший за це (темні сцени не чіпаємо)
    float  MaxColor;
    uint   Mode;        // біт 0 — заміна NaN/чорного, біт 1 — обрізання «світлячків»; 0 — лише рахувати
    float2 InSize;      // розмір вхідної текстури
    float  BrightRatio; // «світлячок»: яскравість виходу > BrightRatio × локальний максимум входу
    float  Pad;
};

float luma(float3 c) { return dot(c, float3(0.2126, 0.7152, 0.0722)); }

[RootSignature(RS)]
[numthreads(8, 8, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    if (id.x >= OutSize.x || id.y >= OutSize.y)
        return;
    uint2 p = id.xy;
    float3 o = Out[p].rgb;
    float2 uv = (float2(p) + 0.5) / float2(OutSize) * UvScale;
    float3 ref = min(InColor.SampleLevel(Lin, uv, 0).rgb, MaxColor);

    // найяскравіше у вхідному кадрі навколо цього місця (3x3 текселі рендер-роздільності)
    uint2 rs = uint2(UvScale * InSize);
    int2 c = int2(uv * InSize);
    float3 mx = 0.0;
    [unroll] for (int dy = -1; dy <= 1; dy++)
    [unroll] for (int dx = -1; dx <= 1; dx++) {
        int2 q = clamp(c + int2(dx, dy), int2(0, 0), int2(rs) - 1);
        mx = max(mx, min(InColor.Load(int3(q, 0)).rgb, MaxColor));
    }

    bool nanOut = any(isnan(o)) || any(isinf(o));
    float lo = nanOut ? 0.0 : luma(o);
    float li = luma(ref);
    bool dark = !nanOut && li > MinLuma && lo < li * DarkRatio;
    // «світлячок»: FSR 4 видав яскравіше, ніж було поруч у вхідному кадрі (зворотне тонування
    // в нейромережі «вибухає» на сонці/небі) — постобробка гри (bloom, автоекспозиція) від такого
    // пікселя затемнює весь кадр → чорний спалах
    float lm = max(luma(mx), 1e-4);
    bool bright = !nanOut && lo > lm * BrightRatio && lo > MinLuma;

    if (nanOut) Counters.InterlockedAdd(16, 1);
    if (dark)   Counters.InterlockedAdd(20, 1);
    if (bright) Counters.InterlockedAdd(28, 1);
    if (id.x == 0 && id.y == 0) Counters.InterlockedAdd(24, 1);

    if ((Mode & 1) != 0 && (nanOut || dark))
        Out[p] = float4(ref, 1.0);
    else if ((Mode & 2) != 0 && bright)
        Out[p] = float4(min(o, mx * BrightRatio), 1.0);   // обрізаємо до локального максимуму входу
    else if ((Mode & 2) != 0 && nanOut)
        Out[p] = float4(ref, 1.0);
}
