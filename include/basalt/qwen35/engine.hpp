// include/basalt/qwen35/engine.hpp - the Qwen3.6-35B-A3B (GGUF architecture qwen35moe) engine path as a library:
// `basalt` hands a qwen35moe model to it at the top of main() (the family switch). See src/qwen35/q36_engine.cu.
#pragma once

namespace basalt::qwen35 {

/// The engine's command line (the main binary's flag names where they mean the same thing: --native / --model,
/// --max-context, --prefill / --chunk, --serve, --mtp N, --tokens, --max-new, --no-token-graph; the
/// family's own: --expert-vram, --expert-slots, --hybrid-rows, --cpu-threads).  Returns the process exit code.
int run(int argc, char** argv);

}  // namespace basalt::qwen35
