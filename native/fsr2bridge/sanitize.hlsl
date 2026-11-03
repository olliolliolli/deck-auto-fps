// AutoFPS FSR2→FSR4 міст: очищення вхідних даних перед FSR 4.
//
// Рідний FSR 2 сам «гасить» погані пікселі (max(0, колір) у підготовці кольору), а FSR 4 — ні:
// один NaN/Inf у кольорі (сонце, яскраве небо, спалахи) отруює автоекспозицію всього кадру
// (кадр блимає чорним) або розповзається чорною плямою через нейромережу. Так само NaN у векторах
// руху (небо без геометрії) і глибина поза [0, 1].
//
// Тут: колір → скінченний, 0..MaxColor; глибина → [0, 1] (і правильний формат перегляду R32_FLOAT
// замість D32S8); вектори → скінченні, обмежені. Лічильники поганих пікселів — для журналу моста.
#define RS "DescriptorTable(SRV(t0, numDescriptors = 3), UAV(u0, numDescriptors = 4)), " \
           "RootConstants(num32BitConstants = 4, b0)"

Texture2D<float4>   InColor  : register(t0);
Texture2D<float>    InDepth  : register(t1);
Texture2D<float2>   InMv     : register(t2);
RWTexture2D<float4> OutColor : register(u0);
RWTexture2D<float>  OutDepth : register(u1);
RWTexture2D<float2> OutMv    : register(u2);
RWByteAddressBuffer Counters : register(u3);

cbuffer Params : register(b0)
{
    uint2 Size;       // розмір текстур (рендер-роздільність)
    float MaxColor;   // стеля яскравості (Inf → стеля)
    float MaxMv;      // стеля векторів руху (в одиницях самих векторів)
};

bool bad(float v) { return isnan(v) || isinf(v); }

[RootSignature(RS)]
[numthreads(8, 8, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    if (id.x >= Size.x || id.y >= Size.y)
        return;
    uint2 p = id.xy;

    float3 c = InColor[p].rgb;
    uint badColor = 0;
    [unroll] for (int i = 0; i < 3; i++) {
        if (isnan(c[i])) { c[i] = 0.0; badColor = 1; }
        else if (isinf(c[i])) { c[i] = c[i] > 0.0 ? MaxColor : 0.0; badColor = 1; }
    }
    OutColor[p] = float4(clamp(c, 0.0, MaxColor), 1.0);

    float d = InDepth[p];
    uint badDepth = 0;
    if (bad(d)) { d = 0.0; badDepth = 1; }
    else if (d < 0.0 || d > 1.0) { badDepth = 1; }
    OutDepth[p] = saturate(d);

    float2 m = InMv[p];
    uint badMv = 0;
    if (bad(m.x) || bad(m.y)) { m = float2(0.0, 0.0); badMv = 1; }
    else if (abs(m.x) > MaxMv || abs(m.y) > MaxMv) { badMv = 1; }
    OutMv[p] = clamp(m, -MaxMv, MaxMv);   // min/max у D3D також прибирають NaN — подвійний захист

    if (badColor) Counters.InterlockedAdd(0, 1);
    if (badDepth) Counters.InterlockedAdd(4, 1);
    if (badMv)    Counters.InterlockedAdd(8, 1);
    if (id.x == 0 && id.y == 0) Counters.InterlockedAdd(12, 1);   // кадрів пройшло через очищення
}
