# wy_led: the LED vocabulary as a host-testable core. No ESP/RTOS includes in the source, so the
# very same file is compiled by scripts/test_esp32_led.py on the host.
idf_component_register(
    SRCS "wy_led.c"
    INCLUDE_DIRS "."
)
