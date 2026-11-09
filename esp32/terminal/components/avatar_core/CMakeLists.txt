# Pure C, no ESP-IDF headers: the viseme timeline and the WebSocket reassembly.
# The same sources are compiled on a PC by test_host/.
idf_component_register(
    SRCS "viseme_timeline.c" "ws_reasm.c"
    INCLUDE_DIRS "include"
)
