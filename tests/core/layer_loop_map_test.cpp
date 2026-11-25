// The layer-loop slot map (host only): every slot takes the weights of a pack layer of the same kind, layers outside the
// window appear once and in order, the window repeats k times, and the map is the identity when the loop is off.
#include "basalt/models/qwen4exp/decode/layout.hpp"

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

using namespace basalt::core;

int main() {
    auto check = [](bool ok, const std::string& what) {
        if (!ok) { std::fprintf(stderr, "layer_loop_map_test: %s\n", what.c_str()); std::exit(1); }
    };

    // off: the identity, whatever a and b hold
    {
        ModelGeometry g;
        for (int64_t s = 0; s < g.n_layers; ++s) {
            check(source_layer(g, s) == s, "off: slot " + std::to_string(s) + " is not its own source");
            check(loop_pass(g, s) == 0, "off: a pass other than 0");
        }
        check(g.file_layers() == 48, "off: the pack has " + std::to_string(g.file_layers()) + " layers");
        g.loop_a = 8;
        g.loop_b = 16;
        g.loop_k = 1;
        for (int64_t s = 0; s < g.n_layers; ++s) check(source_layer(g, s) == s, "k = 1 is not the identity");
    }

    int64_t cases = 0;
    for (int64_t len = 4; len <= 16; len += 4) {
        for (int64_t a = 2; a + len <= 48; ++a) {
            for (int64_t k = 2; k <= 5; ++k) {
                ModelGeometry g;
                g.loop_a = a;
                g.loop_b = a + len;
                g.loop_k = k;
                const int64_t file = g.n_layers;
                g.n_layers = file + (k - 1) * len;
                const std::string tag = " (a " + std::to_string(a) + ", len " + std::to_string(len) + ", k " + std::to_string(k) + ")";

                check(g.n_layers % g.qsa_interval == 0, "the slot count is not a multiple of 4" + tag);
                check(g.file_layers() == file, "the pack layer count is not recovered" + tag);

                std::vector<int64_t> seen(file, 0);
                int64_t previous_outside = -1;
                for (int64_t s = 0; s < g.n_layers; ++s) {
                    const int64_t src = source_layer(g, s);
                    check(src >= 0 && src < file, "a source outside the pack" + tag);
                    check(is_qsa_layer(g, s) == is_qsa_layer(g, src), "slot " + std::to_string(s) + " and its source differ in kind" + tag);
                    ++seen[src];
                    const bool inside = s >= a && s < a + k * len;
                    if (!inside) {
                        check(src > previous_outside, "layers outside the window are out of order" + tag);
                        previous_outside = src;
                        check(loop_pass(g, s) == 0, "a pass inside the window numbering outside it" + tag);
                    } else {
                        check(loop_pass(g, s) == (s - a) / len, "wrong pass number" + tag);
                        check(src == a + (s - a) % len, "the window does not repeat in order" + tag);
                    }
                }
                for (int64_t l = 0; l < file; ++l) {
                    const int64_t want = (l >= a && l < a + len) ? k : 1;
                    check(seen[l] == want, "layer " + std::to_string(l) + " appears " + std::to_string(seen[l]) + " times" + tag);
                }
                check(source_layer(g, 0) == 0 && source_layer(g, 1) == 1, "slots 0 and 1 moved" + tag);
                check(source_layer(g, g.n_layers - 1) == file - 1, "the last slot is not the last pack layer" + tag);
                ++cases;
            }
        }
    }

    // LayerView resolves the weight name through the map
    {
        ModelGeometry g;
        g.loop_a = 8;
        g.loop_b = 16;
        g.loop_k = 3;
        g.n_layers = 48 + 2 * 8;
        WeightTable t;
        set_layer_loop_map(g);
        check(LayerView(t, 7).name("attn_norm.weight") == "blk.7.attn_norm.weight", "a layer before the window was renamed");
        check(LayerView(t, 8).name("x") == "blk.8.x" && LayerView(t, 16).name("x") == "blk.8.x" && LayerView(t, 24).name("x") == "blk.8.x",
              "the same layer of each pass does not share a name");
        check(LayerView(t, 32).name("x") == "blk.16.x", "the first layer after the window does not follow it");
        check(LayerView(t, 63).name("x") == "blk.47.x", "the last slot does not name the last pack layer");
        set_layer_loop_map(ModelGeometry{});
        check(LayerView(t, 16).name("x") == "blk.16.x", "the map was not reset");
    }

    std::printf("layer_loop_map_test: ok (%lld windows)\n", (long long) cases);
    return 0;
}
