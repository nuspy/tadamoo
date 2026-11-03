#include "startup.hpp"
#include "arguments.hpp"
#include "help.hpp"
#include "serve.hpp"

#include "basalt/log.hpp"
#include "basalt/platform/memory.hpp"
#include "basalt/platform/process.hpp"
#include "basalt/platform/system.hpp"
#include "basalt/program/options.hpp"
#if defined(BASALT_HAVE_QWEN35)
#include "basalt/artifact/gguf_reader.hpp"
#include "basalt/artifact/gguf_split.hpp"
#include "basalt/core/model_file.hpp"
#include "basalt/qwen35/engine.hpp"
#endif

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <string>

namespace basalt::program::cli {

namespace {

#if defined(BASALT_HAVE_QWEN35)
/// Whether --model / --native names a GGUF whose architecture is qwen35moe (read from the header of its first shard
/// only), or a .basalt whose meta.family is qwen35moe.
bool names_qwen35_model(int argc, char** argv) {
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::strcmp(argv[i], "--native") != 0 && std::strcmp(argv[i], "--model") != 0) continue;
        const std::string arg = argv[i + 1];
        if (arg.size() > 7 && arg.compare(arg.size() - 7, 7, ".basalt") == 0) {
            basalt::core::ModelFile pack;
            std::string err;
            return pack.open(arg, err) && pack.load_meta(err) && pack.family() == basalt::ModelFamily::Qwen35Moe;
        }
        try {
            const basalt::GgufFile meta(basalt::gguf_split_paths(argv[i + 1]).front());
            return basalt::model_family(meta) == basalt::ModelFamily::Qwen35Moe;
        } catch (const std::exception&) {
            return false;   // not readable as a GGUF here: the normal path reports it
        }
    }
    return false;
}

/// --gpu for the qwen35moe path, which drives one card: the first number of the list, as nvidia-smi numbers them.
void select_qwen35_gpu(int argc, char** argv) {
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::strcmp(argv[i], "--gpu") != 0) continue;
        const std::string first = std::string(argv[i + 1]).substr(0, std::string(argv[i + 1]).find(','));
        if (first.empty() || first.find_first_not_of("0123456789") != std::string::npos) return;
        basalt::platform::set_env("CUDA_DEVICE_ORDER", "PCI_BUS_ID");
        basalt::platform::set_env("CUDA_VISIBLE_DEVICES", first.c_str());
        return;
    }
}
#endif

}  // namespace

std::optional<int> start(int argc, char** argv, Options& o, std::string& ppl_base, std::string& ppl_out) {
    // **UNBUFFERED, BECAUSE THE INTERESTING OUTPUT IS THE OUTPUT BEFORE A CRASH.**  `stdout` redirected to a
    // pipe or a file is block-buffered, so a program that dies loses every line it had already printed - which
    // turns "it crashed at step 7" into "it crashed somewhere", and the difference is a debugging session.
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    if (argc >= 2 && std::strcmp(argv[1], "serve") == 0) return serve_subcommand(argc, argv);
    // if memory runs out the kernel ends this process first, not the rest of the machine
    basalt::platform::prefer_oom_victim();
#if defined(BASALT_HAVE_QWEN35)
    // A Qwen3.6-35B-A3B file (GGUF architecture qwen35moe) runs on its own engine path (src/qwen35), decided from the
    // file's header before any state of this engine exists; every other file goes on below.
    if (names_qwen35_model(argc, argv)) {
        select_qwen35_gpu(argc, argv);
        return basalt::qwen35::run(argc, argv);
    }
#endif
    // the CPU expert pool keeps its speed while the window is in the background
    if (!basalt::platform::keep_full_speed())
        basalt::log::warn("basalt", "full-speed scheduling refused: decode slows while the window is in the background");
    // Kernels load on first use: loading every kernel of the engine and of cuBLAS at context creation holds a few
    // hundred MiB of device memory for code that never runs.  The expert cache's sizing keeps back room for what loads
    // later (`kLazyKernelsMib`).  CUDA_MODULE_LOADING=EAGER restores the up-front load; batched serving sets it (below).
    const bool module_loading_set = std::getenv("CUDA_MODULE_LOADING") != nullptr;
    if (!module_loading_set) basalt::platform::set_env("CUDA_MODULE_LOADING", "LAZY");
    if (const auto code = parse_options(argc, argv, o, ppl_base, ppl_out)) return *code;
    // Batched serving reads a prompt on its own thread while verify windows run.  A lazy kernel load on that thread
    // synchronizes the context and can wait on a window kernel that waits on the load, so batched serving loads every
    // kernel up front.  An explicit CUDA_MODULE_LOADING wins.
    if (o.serve && o.batch >= 2 && !module_loading_set) basalt::platform::set_env("CUDA_MODULE_LOADING", "EAGER");
    return std::nullopt;
}

std::optional<int> prepare_model_process(const Options& o) {
    if (o.model.empty()) {
        basalt::log::error("basalt generate", "--model FILE.basalt is required");
        usage();
        return 2;
    }
    // a load that exhausts the RAM ends here instead of swapping the machine to a halt; stopped at READY
    if (o.serve) basalt::platform::start_memory_watchdog(1ull << 30);
    // the serve protocol's pipes carry bytes, so its newlines are the engine's own
    if (o.serve) basalt::platform::set_stdio_binary();
    return std::nullopt;
}

}  // namespace basalt::program::cli
