idf_component_register(SRCS "main.c" "provisioning_console.c" "usb_wyoming.c" "wifi_station.c"
                            "gateway_socket.c" "gateway_link.c" "gateway_tcp.c" "gateway_tls.c"
                            "device_identity.c" "time_sync.c" "tls_credentials.c"
                            "tls_credentials_store.c" INCLUDE_DIRS "."
    REQUIRES wyoming_diagnostic memory_probe provisioning wyoming_session wifi_report gateway_supervisor esp_psram heap freertos log
             nvs_flash esp_driver_usb_serial_jtag esp_hw_support esp_timer esp_wifi esp_netif esp_event lwip
             mbedtls esp_partition)
