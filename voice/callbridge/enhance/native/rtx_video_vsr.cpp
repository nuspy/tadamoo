// This software contains source code provided by NVIDIA Corporation.
//
// A derivative of NVIDIA's own RTX Video SDK 1.1.0 CUDA sample (samples/RTX_Video_API/rtx_video_api_cuda_impl.cpp,
// the simplified API the SDK's Programming Guide names for integrators with no existing graphics device - see
// docs/voice/acceleration.md). Licensed to distribute in object code form, as part of this application, under the
// NVIDIA RTX SDKs License (v. February 23, 2024) section 1(b)-(c) and 2: this application has material functionality
// well beyond the SDK, and this file is not distributed as the SDK itself.
//
// Two differences from NVIDIA's sample, both deliberate:
//   - only VSR (upscaling) is wired up; TrueHDR (HDR tone mapping) is a different feature this project does not use.
//   - every CUDA/NGX failure returns 0 with a message from tadamoo_rtx_vsr_last_error(): the sample's own macros call
//     exit()/getchar() on any error, which would take the whole voice service down with it.
//
// Needs the SDK's own headers/libs (NV_RTX_VIDEO_SDK) and the CUDA Toolkit to build; see
// tools/windows/build_rtx_video_native.ps1. Nothing here is itself the SDK: this file alone is useless without it.

#include <cuda.h>
#include <nvsdk_ngx.h>
#include <nvsdk_ngx_defs_vsr.h>

#include <cstdio>
#include <cstring>
#include <string>

#define TADAMOO_RTX_API extern "C" __declspec(dllexport)

namespace {

std::string g_last_error = "not created";
CUcontext g_cu_context = nullptr;
CUdevice g_cu_device = 0;
NVSDK_NGX_Parameter* g_params = nullptr;
NVSDK_NGX_Handle* g_vsr_feature = nullptr;

CUarray g_src_array = nullptr;
CUtexObject g_src_tex = 0;
int g_src_w = 0, g_src_h = 0;

CUarray g_dst_array = nullptr;
CUsurfObject g_dst_surf = 0;
int g_dst_w = 0, g_dst_h = 0;

void set_error(const char* what, int code) {
    char buf[256];
    std::snprintf(buf, sizeof buf, "%s (code %d)", what, code);
    g_last_error = buf;
}

void set_cuda_error(const char* what, CUresult r) {
    const char* text = nullptr;
    cuGetErrorString(r, &text);
    char buf[256];
    std::snprintf(buf, sizeof buf, "%s: %s", what, text ? text : "unknown CUDA error");
    g_last_error = buf;
}

void destroy_src() {
    if (g_src_tex) { cuTexObjectDestroy(g_src_tex); g_src_tex = 0; }
    if (g_src_array) { cuArrayDestroy(g_src_array); g_src_array = nullptr; }
    g_src_w = g_src_h = 0;
}

void destroy_dst() {
    if (g_dst_surf) { cuSurfObjectDestroy(g_dst_surf); g_dst_surf = 0; }
    if (g_dst_array) { cuArrayDestroy(g_dst_array); g_dst_array = nullptr; }
    g_dst_w = g_dst_h = 0;
}

// A WxH array of 4-channel 8-bit pixels (BGRA, matching the numpy frames this provider receives), with a texture
// object to read it (source) or a surface object to write it (destination) - the shape NGX_CUDA_EVALUATE_VSR needs.
bool make_array(int w, int h, bool as_surface, CUarray* out_array, CUtexObject* out_tex, CUsurfObject* out_surf) {
    CUDA_ARRAY_DESCRIPTOR desc{};
    desc.Width = static_cast<size_t>(w);
    desc.Height = static_cast<size_t>(h);
    desc.Format = CU_AD_FORMAT_UNSIGNED_INT8;
    desc.NumChannels = 4;
    CUresult r = cuArrayCreate(out_array, &desc);
    if (r != CUDA_SUCCESS) { set_cuda_error("cuArrayCreate", r); return false; }

    CUDA_RESOURCE_DESC res{};
    res.resType = CU_RESOURCE_TYPE_ARRAY;
    res.res.array.hArray = *out_array;

    if (as_surface) {
        r = cuSurfObjectCreate(out_surf, &res);
        if (r != CUDA_SUCCESS) { set_cuda_error("cuSurfObjectCreate", r); return false; }
        return true;
    }
    CUDA_TEXTURE_DESC tex{};
    tex.addressMode[0] = tex.addressMode[1] = tex.addressMode[2] = CU_TR_ADDRESS_MODE_CLAMP;
    tex.filterMode = CU_TR_FILTER_MODE_LINEAR;
    tex.flags = CU_TRSF_NORMALIZED_COORDINATES;
    r = cuTexObjectCreate(out_tex, &res, &tex, nullptr);
    if (r != CUDA_SUCCESS) { set_cuda_error("cuTexObjectCreate", r); return false; }
    return true;
}

}  // namespace

TADAMOO_RTX_API int tadamoo_rtx_vsr_create(int gpu_index) {
    CUresult cr = cuInit(0);
    if (cr != CUDA_SUCCESS) { set_cuda_error("cuInit", cr); return 0; }
    cr = cuDeviceGet(&g_cu_device, gpu_index);
    if (cr != CUDA_SUCCESS) { set_cuda_error("cuDeviceGet", cr); return 0; }
    cr = cuDevicePrimaryCtxRetain(&g_cu_context, g_cu_device);
    if (cr != CUDA_SUCCESS) { set_cuda_error("cuDevicePrimaryCtxRetain", cr); return 0; }

    NVSDK_NGX_Result nr = NVSDK_NGX_CUDA_Init(0, L".");
    if (nr != NVSDK_NGX_Result_Success) { set_error("NVSDK_NGX_CUDA_Init", (int)nr); return 0; }

    nr = NVSDK_NGX_CUDA_GetCapabilityParameters(&g_params);
    if (nr != NVSDK_NGX_Result_Success || !g_params) { set_error("NVSDK_NGX_CUDA_GetCapabilityParameters", (int)nr); return 0; }

    int available = 0;
    nr = g_params->Get(NVSDK_NGX_Parameter_VSR_Available, &available);
    if (nr != NVSDK_NGX_Result_Success || !available) { g_last_error = "VSR is not available on this system/driver"; return 0; }

    NVSDK_NGX_Parameter_SetVoidPointer(g_params, NVSDK_NGX_Parameter_Input1, g_cu_context);
    NVSDK_NGX_Parameter_SetVoidPointer(g_params, NVSDK_NGX_Parameter_Input2, nullptr);  // the primary context's own stream
    nr = NVSDK_NGX_CUDA_CreateFeature(NVSDK_NGX_Feature_VSR, g_params, &g_vsr_feature);
    if (nr != NVSDK_NGX_Result_Success) { set_error("NVSDK_NGX_CUDA_CreateFeature(VSR)", (int)nr); return 0; }

    g_last_error.clear();
    return 1;
}

// bgra_in/bgra_out: BGRA8 pixels, row-major, stride exactly 4*width (no padding) - the format `rtx_video.py` converts
// its BGR frames to before calling this. quality: 0 (bicubic) to 4 (ultra, slowest); the SDK's own range.
TADAMOO_RTX_API int tadamoo_rtx_vsr_evaluate(const unsigned char* bgra_in, int in_w, int in_h,
                                              unsigned char* bgra_out, int out_w, int out_h, int quality) {
    if (!g_vsr_feature) { g_last_error = "not created"; return 0; }

    if (!g_src_array || g_src_w != in_w || g_src_h != in_h) {
        destroy_src();
        if (!make_array(in_w, in_h, false, &g_src_array, &g_src_tex, nullptr)) return 0;
        g_src_w = in_w; g_src_h = in_h;
    }
    if (!g_dst_array || g_dst_w != out_w || g_dst_h != out_h) {
        destroy_dst();
        if (!make_array(out_w, out_h, true, &g_dst_array, nullptr, &g_dst_surf)) return 0;
        g_dst_w = out_w; g_dst_h = out_h;
    }

    CUDA_MEMCPY2D up{};
    up.srcMemoryType = CU_MEMORYTYPE_HOST;
    up.srcHost = bgra_in;
    up.srcPitch = static_cast<size_t>(in_w) * 4;
    up.dstMemoryType = CU_MEMORYTYPE_ARRAY;
    up.dstArray = g_src_array;
    up.WidthInBytes = static_cast<size_t>(in_w) * 4;
    up.Height = static_cast<size_t>(in_h);
    CUresult cr = cuMemcpy2D(&up);
    if (cr != CUDA_SUCCESS) { set_cuda_error("cuMemcpy2D (upload)", cr); return 0; }

    NVSDK_NGX_Parameter_SetVoidPointer(g_params, NVSDK_NGX_Parameter_Input1, &g_src_tex);
    NVSDK_NGX_Parameter_SetVoidPointer(g_params, NVSDK_NGX_Parameter_Output, &g_dst_surf);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_Rect_X, 0);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_Rect_Y, 0);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_Rect_W, (unsigned)in_w);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_Rect_H, (unsigned)in_h);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_OutRect_X, 0);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_OutRect_Y, 0);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_OutRect_W, (unsigned)out_w);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_OutRect_H, (unsigned)out_h);
    NVSDK_NGX_Parameter_SetUI(g_params, NVSDK_NGX_Parameter_VSR_QualityLevel, (unsigned)quality);
    NVSDK_NGX_Result nr = NVSDK_NGX_CUDA_EvaluateFeature_C(g_vsr_feature, g_params, nullptr);
    if (nr != NVSDK_NGX_Result_Success) { set_error("NVSDK_NGX_CUDA_EvaluateFeature(VSR)", (int)nr); return 0; }

    CUDA_MEMCPY2D down{};
    down.srcMemoryType = CU_MEMORYTYPE_ARRAY;
    down.srcArray = g_dst_array;
    down.dstMemoryType = CU_MEMORYTYPE_HOST;
    down.dstHost = bgra_out;
    down.dstPitch = static_cast<size_t>(out_w) * 4;
    down.WidthInBytes = static_cast<size_t>(out_w) * 4;
    down.Height = static_cast<size_t>(out_h);
    cr = cuMemcpy2D(&down);
    if (cr != CUDA_SUCCESS) { set_cuda_error("cuMemcpy2D (download)", cr); return 0; }

    return 1;
}

TADAMOO_RTX_API void tadamoo_rtx_vsr_shutdown() {
    if (g_vsr_feature) { NVSDK_NGX_CUDA_ReleaseFeature(g_vsr_feature); g_vsr_feature = nullptr; }
    destroy_src();
    destroy_dst();
    if (g_params) { NVSDK_NGX_CUDA_DestroyParameters(g_params); g_params = nullptr; }
    NVSDK_NGX_CUDA_Shutdown();
    if (g_cu_context) { cuDevicePrimaryCtxRelease(g_cu_device); g_cu_context = nullptr; }
    g_last_error = "not created";
}

TADAMOO_RTX_API const char* tadamoo_rtx_vsr_last_error() {
    return g_last_error.c_str();
}
