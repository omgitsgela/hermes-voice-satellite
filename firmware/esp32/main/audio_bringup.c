#include "audio_bringup.h"

#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

/* The XVF3800 <-> XIAO ESP32-S3 wiring, as documented by Seeed (I2C on SDA=GPIO5 / SCL=GPIO6,
 * I2S master with BCLK=GPIO8, WS=GPIO7, data out GPIO44, data in GPIO43; stereo 32-bit 16 kHz).
 * These are constants, not decisions: the bring-up is for this board and its pins. */
#define XVF_I2C_SDA_PIN        GPIO_NUM_5
#define XVF_I2C_SCL_PIN        GPIO_NUM_6
#define XVF_I2C_SCL_HZ         400000
#define XVF_I2C_ADDRESS        0x2C
#define XVF_I2S_PORT           I2S_NUM_0
#define XVF_I2S_BCLK_PIN       GPIO_NUM_8
#define XVF_I2S_WS_PIN         GPIO_NUM_7
#define XVF_I2S_TX_PIN         GPIO_NUM_44
#define XVF_I2S_RX_PIN         GPIO_NUM_43
/* The 48 kHz profile: the I2S link runs at 48 kHz in both directions, matching the Seeed
 * reference (docs/audio-fidelity.md). CLOCK OWNER: the XVF3800 co-processor, not us. Upstream's
 * I2S images are either a 48 kHz MASTER (it drives BCLK/WS, the host must be a slave) or a 16 kHz
 * SLAVE (the host drives them); "the two devices must use complementary roles" -- two masters
 * contend for the clock, two slaves leave the bus unclocked. This probe is a slave so it measures
 * the same arrangement the live channels use. */
#define XVF_I2S_ROLE_PROBE     I2S_ROLE_SLAVE
#define XVF_I2S_RATE_HZ        48000

/* Application Servicer (system) VERSION read: resid 48, cmd 0, three bytes. The I2C read protocol
 * is a 3-byte write [resid, cmd|0x80, read_len+1] followed by [status, data...]; status 0 is
 * success and 64 asks for a retry. */
#define XVF_RESID_APP          48
#define XVF_CMD_VERSION        0
#define XVF_STATUS_RETRY       64
#define XVF_I2C_TIMEOUT_MS     100

/* Step 1: prove the I2S interface initializes for the XVF3800. The channel is created and
 * initialized to standard mode, which is the interface bring-up; it is not started, so no stream
 * runs. It is deleted on every exit path. */
static int i2s_interface_up(void) {
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(XVF_I2S_PORT, XVF_I2S_ROLE_PROBE);
    i2s_chan_handle_t tx = NULL;
    i2s_chan_handle_t rx = NULL;
    if (i2s_new_channel(&channel, &tx, &rx) != ESP_OK) return AUDIO_I2S_FAIL;

    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(XVF_I2S_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = XVF_I2S_BCLK_PIN,
            .ws   = XVF_I2S_WS_PIN,
            .dout = XVF_I2S_TX_PIN,
            .din  = XVF_I2S_RX_PIN,
        },
    };
    int result = (i2s_channel_init_std_mode(tx, &std) == ESP_OK &&
                  i2s_channel_init_std_mode(rx, &std) == ESP_OK)
                 ? AUDIO_I2S_OK : AUDIO_I2S_FAIL;
    /* The channel is a bring-up proof, not a running stream. Release it on every path; never leave
     * a half-initialized controller behind. */
    i2s_del_channel(tx);
    i2s_del_channel(rx);
    return result;
}

/* Step 2: prove the codec is there and read its firmware version. A no-answer is a real, reportable
 * outcome (no_codec), not an error to hide; a bus fault is. The bus is torn down on every path. */
static int i2c_codec_probe(unsigned char version_out[3], int *version_present) {
    i2c_master_bus_handle_t bus = NULL;
    i2c_master_dev_handle_t dev = NULL;
    *version_present = 0;

    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = (i2c_port_num_t)-1,   /* -1 selects the first free I2C instance */
        .sda_io_num = XVF_I2C_SDA_PIN,
        .scl_io_num = XVF_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    if (i2c_new_master_bus(&bus_cfg, &bus) != ESP_OK) return AUDIO_I2C_BUS_ERROR;

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XVF_I2C_ADDRESS,
        .scl_speed_hz = XVF_I2C_SCL_HZ,
        .scl_wait_us = 0,
        .flags.disable_ack_check = false,
    };
    if (i2c_master_bus_add_device(bus, &dev_cfg, &dev) != ESP_OK) {
        i2c_del_master_bus(bus);
        return AUDIO_I2C_BUS_ERROR;
    }

    /* A probe at the codec's address: if nothing answers there, the chain is not live. */
    if (i2c_master_probe(bus, XVF_I2C_ADDRESS, XVF_I2C_TIMEOUT_MS) != ESP_OK) {
        i2c_master_bus_rm_device(dev);
        i2c_del_master_bus(bus);
        return AUDIO_I2C_NO_CODEC;
    }

    /* VERSION read: write [resid, cmd|0x80, 4] then read [status, maj, min, patch], retrying on the
     * codec's retry status. */
    uint8_t cmd[3] = { (uint8_t)XVF_RESID_APP, (uint8_t)(XVF_CMD_VERSION | 0x80), 4 };
    uint8_t reply[4] = {0};
    int code = AUDIO_I2C_OK;
    int attempts = 0;
    while (attempts++ < 4) {
        if (i2c_master_transmit_receive(dev, cmd, sizeof(cmd), reply, sizeof(reply),
                                        XVF_I2C_TIMEOUT_MS) != ESP_OK) {
            code = AUDIO_I2C_BUS_ERROR;
            break;
        }
        if (reply[0] == XVF_STATUS_RETRY) continue;   /* the codec asked to be retried */
        if (reply[0] == 0) {
            version_out[0] = reply[1];
            version_out[1] = reply[2];
            version_out[2] = reply[3];
            *version_present = 1;
        }
        break;   /* a definitive status, good or otherwise */
    }

    i2c_master_bus_rm_device(dev);
    i2c_del_master_bus(bus);
    return code;
}

int audio_bringup_run(char *out, size_t capacity) {
    if (!out || capacity == 0) return -1;
    audio_bringup_result result = {
        .i2s = AUDIO_I2S_FAIL,
        .i2c = AUDIO_I2C_BUS_ERROR,
        .version_present = 0,
    };

    result.i2s = i2s_interface_up();

    /* The codec probe is independent of whether the I2S interface came up: a board whose I2S pin
     * map is wrong can still have a live codec, and reporting both steps is the useful answer. */
    result.i2c = i2c_codec_probe(result.version, &result.version_present);

    audio_bringup_render(&result, out, capacity);
    return 0;
}
