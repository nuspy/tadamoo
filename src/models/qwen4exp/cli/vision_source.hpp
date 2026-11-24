// Where the image encoder reads its projector from.
#pragma once

#include "basalt/core/model_file.hpp"
#include "basalt/models/qwen4exp/options.hpp"
#include "basalt/platform/file.hpp"

namespace basalt::models::qwen4exp::cli {

/// The file --vision-mmproj names, whole, else the model file's own vision section (`pack_section`).
inline basalt::core::FileRange vision_source(const Options& o, const basalt::core::FileRange& pack_section) {
    if (o.vision_mmproj.empty()) return pack_section;
    uint64_t bytes = 0;
    basalt::platform::file_size(o.vision_mmproj, bytes);
    return basalt::core::FileRange(o.vision_mmproj, 0, bytes);
}

}  // namespace basalt::models::qwen4exp::cli
