#ifndef MEMORY_PROBE_H
#define MEMORY_PROBE_H
#include <stddef.h>
#include <stdint.h>
void memory_probe_fill(volatile uint8_t *buffer, size_t length, uint8_t seed);
int memory_probe_check(const volatile uint8_t *buffer, size_t length, uint8_t seed);
#endif
