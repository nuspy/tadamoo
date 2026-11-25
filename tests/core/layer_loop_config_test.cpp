// The layer-loop flags (host only): their form, the refusals that need no model, what they do to the geometry, the write scale
// each slot gets, and the words that keep two configurations' conversations apart.
#include "basalt/models/qwen4exp/decode/layout.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <set>
#include <string>
#include <vector>

using namespace basalt::core;

int main() {
    auto check = [](bool ok, const std::string& what) {
        if (!ok) { std::fprintf(stderr, "layer_loop_config_test: %s\n", what.c_str()); std::exit(1); }
    };
    auto refused = [&](const char* window, const char* alphas, const char* word) {
        LayerLoopSpec s;
        std::string err;
        const bool ok = parse_layer_loop(window, alphas, s, err);
        check(!ok, std::string("accepted: ") + window + " / " + alphas);
        check(err.find(word) != std::string::npos, std::string("'") + window + "' / '" + alphas + "': the message '" + err + "' does not say '" + word + "'");
    };

    // the form
    refused("8-16-2", "", "A:B:K");
    refused("8:16", "", "A:B:K");
    refused("8:16:2:1", "", "A:B:K");
    refused("a:16:2", "", "A:B:K");
    refused("-8:16:2", "", "A:B:K");
    refused("8:16:0", "", "K is");
    refused("8:16:9", "", "K is");
    refused("1:9:2", "", "layer 2");
    refused("0:8:2", "", "layer 2");
    refused("16:8:2", "", "below B");
    refused("8:8:2", "", "below B");
    refused("8:16:2", "1", "needs 2 values, got 1");
    refused("8:16:2", "1,1,1", "needs 2 values, got 3");
    refused("8:16:2", "1,x", "'x'");
    refused("8:16:2", "1,-0.5", "-0.5");
    refused("8:16:2", "1,nan", "nan");
    refused("8:16:2", "1,inf", "inf");
    refused("8:16:2", "1,", "''");
    refused("", "1,0", "needs --layer-loop");

    // accepted
    {
        LayerLoopSpec s;
        std::string err;
        check(parse_layer_loop("", "", s, err) && !s.on(), "no flag is off");
        check(parse_layer_loop("8:16:1", "", s, err) && !s.on(), "K = 1 is off");
        check(parse_layer_loop("8:16:2", "", s, err) && s.on() && s.a == 8 && s.b == 16 && s.k == 2 && s.alpha[0] == 1.0f && s.alpha[1] == 1.0f,
              "8:16:2 with no alphas is 1,1");
        check(parse_layer_loop("20:36:3", "1,0.5,0.25", s, err) && s.k == 3 && s.alpha[1] == 0.5f && s.alpha[2] == 0.25f, "alphas read");
        check(parse_layer_loop("44:48:8", "1,1,1,1,1,1,1,0", s, err) && s.k == 8 && s.alpha[7] == 0.0f, "K = 8, a window to the end");
    }

    // what the spec does to the geometry
    {
        ModelGeometry g;
        LayerLoopSpec s;
        std::string err;
        check(parse_layer_loop("8:16:2", "1,0.5", s, err), err);
        check(apply_layer_loop(g, s, err), err);
        check(g.n_layers == 56 && g.loop_a == 8 && g.loop_b == 16 && g.loop_k == 2 && g.file_layers() == 48, "geometry after 8:16:2");
        check(layer_loop_text(g) == "layer loop: [8,16) x2, alpha 1,0.5, 56 slots", "log line: " + layer_loop_text(g));
        check(mapped_source(20) == 12 && mapped_source(16) == 8 && mapped_source(24) == 16, "the process map follows the geometry");
        // the write scale: 1 outside, the pass's alpha inside, the same for a slot's two halves (one value per slot)
        for (int64_t slot = 0; slot < g.n_layers; ++slot) {
            const float want = slot < 8 || slot >= 24 ? 1.0f : (slot < 16 ? 1.0f : 0.5f);
            check(write_scale(g, slot) == want, "write scale of slot " + std::to_string(slot));
        }
        set_layer_loop_map(ModelGeometry{});

        // a window that does not fit
        ModelGeometry g2;
        check(parse_layer_loop("44:52:2", "", s, err), err);
        check(!apply_layer_loop(g2, s, err) && err.find("past") != std::string::npos && g2.n_layers == 48 && g2.loop_k == 1, "a window past the model");
        check(parse_layer_loop("8:14:2", "", s, err), err);
        check(!apply_layer_loop(g2, s, err) && err.find("multiple of 4") != std::string::npos && g2.n_layers == 48 && g2.loop_k == 1,
              "a window that is not a multiple of 4 (the geometry stays as it was)");
        // off changes nothing
        check(parse_layer_loop("8:16:1", "", s, err) && apply_layer_loop(g2, s, err) && g2.n_layers == 48 && write_scale(g2, 10) == 1.0f, "K = 1 applies as no loop");
    }

    // the words of the identity: every part of the configuration changes them
    {
        auto key_of = [&](const char* w, const char* a) {
            ModelGeometry g;
            LayerLoopSpec s;
            std::string err;
            check(parse_layer_loop(w, a, s, err) && apply_layer_loop(g, s, err), err);
            set_layer_loop_map(ModelGeometry{});
            std::vector<int64_t> k(3 + kLoopMaxK);
            int64_t arr[3 + kLoopMaxK];
            layer_loop_key(g, arr);
            k.assign(arr, arr + 3 + kLoopMaxK);
            return k;
        };
        std::set<std::vector<int64_t>> seen;
        // same slot count (56) from two windows and two alphas, plus other counts
        for (const auto& c : std::vector<std::pair<const char*, const char*>>{
                 {"8:16:2", "1,1"}, {"16:24:2", "1,1"}, {"8:16:2", "1,0.5"}, {"8:16:2", "0.5,1"}, {"8:12:3", "1,1,1"}, {"8:12:3", "1,1,0.5"}})
            check(seen.insert(key_of(c.first, c.second)).second, std::string("two configurations share a key: ") + c.first + " " + c.second);
        check(key_of("8:16:2", "1,1") == key_of("8:16:2", ""), "no alphas hash as 1 each");
    }

    std::printf("layer_loop_config_test: ok\n");
    return 0;
}
