// Load the --lora/--lora-scaled adapters selected by the CLI.
#pragma once

#include <cstddef>
#include <string>

namespace basalt::core {
class LoraSet;
}
namespace basalt::program {
struct Options;
}

namespace basalt::program::cli {

/// Opens every adapter in `o.lora_files` and gives NativeDense the resulting set (NativeDense::set_lora). The
/// adapters and the set are owned here, with process lifetime: NativeDense only keeps a pointer to them, read
/// by every plan_bytes/load call of every later stage, not just the first. Must run before any of those calls
/// -- right after the options are parsed, before GPU selection or the model file is even opened: unlike a
/// control vector, nothing a LoRA merge needs depends on either. `summary` is what an INFO log line reports;
/// `skipped` is the total tensor count across every adapter that `--no-lora-partial` would have refused (0
/// when every adapter is fully covered, or there are none). An empty `o.lora_files` sets no adapters (today's
/// behaviour, unchanged).
bool load_lora(const Options& o, std::string& summary, size_t& skipped, std::string& err);

/// The set `load_lora` opened, or null when no adapter was given.
const basalt::core::LoraSet* lora_set();

}  // namespace basalt::program::cli
