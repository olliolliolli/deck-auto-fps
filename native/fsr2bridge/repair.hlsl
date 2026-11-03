// AutoFPS FSR2→FSR4 міст: «ремонт» виходу FSR 4 — останній рубіж проти чорних спалахів.
//
// Після FSR 4 перевіряємо кожен піксель результату. Якщо він NaN/Inf (нейромережа «вибухнула»)
// або майже чорний там, де вхідний кадр гри яскравий (зіпсована експозиція/історія), — ставимо
// замість нього просте збільшення вхідного кадру (білінійно). Справжні темні деталі не чіпаємо:
// заміна лише коли результат темніший за вхід у десятки разів.
#define RS "DescriptorTable(SRV(t0, numDescriptors = 1), UAV(u0, numDescriptors = 2)), " \
           "RootConstants(num32BitConstants = 8, b0), " \
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
    uint   Enable;      // 0 — лише рахувати, нічого не міняти
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

    bool nanOut = any(isnan(o)) || any(isinf(o));
    float lo = nanOut ? 0.0 : luma(o);
    float li = luma(ref);
    bool dark = !nanOut && li > MinLuma && lo < li * DarkRatio;

    if (nanOut) Counters.InterlockedAdd(16, 1);
    if (dark)   Counters.InterlockedAdd(20, 1);
    if (id.x == 0 && id.y == 0) Counters.InterlockedAdd(24, 1);

    if (Enable != 0 && (nanOut || dark))
        Out[p] = float4(ref, 1.0);
}
