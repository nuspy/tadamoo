#include "lora.hpp"

#include "basalt/core/lora.hpp"
#include "basalt/core/native_dense.hpp"
#include "basalt/program/options.hpp"

#include <vector>

namespace basalt::program::cli {

namespace {
// Process lifetime: NativeDense::set_lora keeps a pointer to `g_set`, whose entries point into `g_adapters`.
std::vector<basalt::core::LoraAdapter> g_adapters;
basalt::core::LoraSet g_set;
}  // namespace

bool load_lora(const Options& o, std::string& summary, size_t& skipped, std::string& err) {
    skipped = 0;
    if (o.lora_files.empty()) return basalt::core::NativeDense::set_lora(nullptr, o.lora_partial, err);
    g_adapters.clear();
    g_adapters.reserve(o.lora_files.size());
    for (const auto& [path, scale] : o.lora_files) {
        basalt::core::LoraAdapter a;
        if (!basalt::core::LoraAdapter::open(path, a, err)) return false;
        skipped += (size_t) a.router_count() + (size_t) a.expert_count() + (size_t) a.other_count();
        g_adapters.push_back(std::move(a));
    }
    std::vector<basalt::core::LoraEntry> entries;
    entries.reserve(g_adapters.size());
    for (size_t i = 0; i < g_adapters.size(); ++i) entries.push_back({&g_adapters[i], o.lora_files[i].second});
    g_set = basalt::core::LoraSet(std::move(entries));
    if (!basalt::core::NativeDense::set_lora(&g_set, o.lora_partial, err)) return false;
    summary = std::to_string(g_adapters.size());   // a bare count: the INFO line splits on whitespace, like cvec's
    return true;
}

}  // namespace basalt::program::cli
