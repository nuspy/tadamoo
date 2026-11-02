// include/basalt/artifact/model_family.hpp - which model family a GGUF holds, from its general.architecture.
//
// Basalt's own engine runs one model (Qwen3.8-Flash-Next, `qwen4exp`).  Qwen3.6-35B-A3B (`qwen35moe`) has an engine
// path of its own (src/qwen35) that the main binary switches to by the GGUF's architecture.
//
// No dependencies: the GGUF reader (the architecture guard) and the core geometry both include this.
#pragma once

#include <string>

namespace basalt {

enum class ModelFamily {
    Unknown = 0,
    Qwen4Exp,    ///< Qwen3.8-Flash-Next and its fine-tunes / pruned variants (Swift 1.5, the Coder)
    Qwen35Moe,   ///< Qwen3.6-35B-A3B (Unsloth's MTP GGUFs): plain pre-norm residual, dense attention, nextn MTP block
};

inline ModelFamily model_family_from_arch(const std::string& arch) {
    if (arch == "qwen4exp") return ModelFamily::Qwen4Exp;
    if (arch == "qwen35moe") return ModelFamily::Qwen35Moe;
    return ModelFamily::Unknown;
}

/// The GGUF architecture string (also the metadata key prefix: "<arch>.block_count").
inline const char* model_family_arch(ModelFamily f) {
    switch (f) {
    case ModelFamily::Qwen4Exp:
        return "qwen4exp";
    case ModelFamily::Qwen35Moe:
        return "qwen35moe";
    default:
        return "";
    }
}

/// The model's name for messages.
inline const char* model_family_title(ModelFamily f) {
    switch (f) {
    case ModelFamily::Qwen4Exp:
        return "Qwen3.8-Flash-Next";
    case ModelFamily::Qwen35Moe:
        return "Qwen3.6-35B-A3B";
    default:
        return "an unknown model";
    }
}

/// Whether the family has an engine path: the qwen4exp one (src/core) or the qwen35moe one (src/qwen35).
inline bool model_family_runs(ModelFamily f) { return f != ModelFamily::Unknown; }

/// The refusal for an architecture that is not a known family (empty when the family runs).
inline std::string model_family_not_supported(ModelFamily f) {
    if (model_family_runs(f)) return {};
    return "this is not a model family Basalt knows";
}

}  // namespace basalt
