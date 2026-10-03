#include "memory_probe.h"

static uint8_t pattern(size_t index, uint8_t seed) {
    return (uint8_t)(seed ^ index ^ (index >> 8) ^ (index >> 16));
}

void memory_probe_fill(volatile uint8_t *buffer, size_t length, uint8_t seed) {
    for (size_t i = 0; i < length; ++i) buffer[i] = pattern(i, seed);
}

int memory_probe_check(const volatile uint8_t *buffer, size_t length, uint8_t seed) {
    for (size_t i = 0; i < length; ++i) {
        if (buffer[i] != pattern(i, seed)) return -1;
    }
    return 0;
}
