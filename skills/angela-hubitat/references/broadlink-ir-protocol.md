# Broadlink IR Protocol Analysis

## How Broadlink Encodes IR Codes

The python-broadlink library (`broadlink/remote.py`) defines the encoding:

### Hex String Format

```
[header 4 bytes] [timing bytes...] [00 0D 05 00 00 00 00 00 00] [checksum 8 bytes]
```

- **Header**: `26 00 length_low length_high` (0x26 = IR mode, 0x00 = zero time)
- **Body**: timing values encoded as raw bytes (with escape coding)
- **Marker**: `00 0D 05 00 00 00 00 00 00` separates body from checksum
- **Checksum**: trailing 8 bytes (hash/CRC of the packet)

### Timing Value Encoding

Each byte in the body is a timing value in units of **32.84 microseconds** (`tick = 32.84`).

Values > 255 use **escape coding**: a `0x00` byte followed by 2 bytes (big-endian 16-bit value).

```python
# From broadlink/remote.py:
def data_to_pulses(data: bytes, tick: float = 32.84) -> List[int]:
    result = []
    index = 4  # skip header
    end = min(256 * data[3] + data[2] + 4, len(data))
    while index < end:
        chunk = data[index]
        index += 1
        if chunk == 0:
            chunk = 256 * data[index] + data[index + 1]
            index += 2
        result.append(int(chunk * tick))
    return result
```

**IMPORTANT**: The `00 01` bytes in Broadlink hex are NOT separators. They encode a single timing value of 256 units = 8,407 microseconds (an inter-frame gap). This is a common misinterpretation.

### Pulse Sequence

The decoded pulse sequence is alternating **mark-space** pairs:
- Index 0, 2, 4... = **mark** durations (IR LED on)
- Index 1, 3, 5... = **space** durations (IR LED off)

### Converting Hex to Pulses (Python)

```python
TICK = 32.84

def hex_to_pulses(hex_str):
    data = bytes.fromhex(hex_str)
    pulses = []
    index = 4
    end = min(256 * data[3] + data[2] + 4, len(data))
    while index < end:
        chunk = data[index]
        index += 1
        if chunk == 0:
            chunk = 256 * data[index] + data[index + 1]
            index += 2
        pulses.append(round(chunk * TICK))
    return pulses

def pulses_to_hex(pulses, tick=32.84):
    result = bytearray(4)
    result[0] = 0x26
    for pulse in pulses:
        div, mod = divmod(int(pulse // tick), 256)
        if div:
            result.append(0)
            result.append(div)
        result.append(mod)
    data_len = len(result) - 4
    result[2] = data_len & 0xFF
    result[3] = data_len >> 8
    return result.hex()
```

## Small Fan IR Protocol (Michael's Fan)

### Encoding Type

**Mark-distance encoding** with two mark durations:
- Short mark (~400us, raw byte ~12) = binary `0`
- Long mark (~1350us, raw byte ~41) = binary `1`
- Spaces separate bits but don't carry data (short ~430us, long ~1300us)

### 12-Bit Repeated Code Structure

Every button press sends the **same 12-bit pattern repeated N times**:

```
DDDD FFFF 0000
|    |    +-- Padding (always 0)
|    +------- Function code (4 bits)
+------------ Device address (always 1101 = 0xD)
```

### Known Function Codes

| Function    | Binary  | Hex | Repeats | Notes                          |
|-------------|---------|-----|---------|--------------------------------|
| Power       | `0010`  | 0x2 | 8       | Toggle on/off                  |
| Max         | `1010`  | 0xA | 4       | Set to speed 8 (highest)       |
| Speed       | `0110`  | 0x6 | 4       | Cycle: 8-1-2-3-4-5-6-7-8-...  |
| Oscillate   | `0100`  | 0x4 | 6       | Toggle oscillation             |
| Light       | `1000`  | 0x8 | 6       | Toggle light                   |
| Timer       | `1100`  | 0xC | 6       | Cycle timer (1h-2h-4h-off)    |

### Repeat Count Pattern

- **4 repeats**: Speed-setting commands (Max, Speed)
- **6 repeats**: Auxiliary functions (Oscillate, Light, Timer)
- **8 repeats**: Power toggle (ensures the toggle registers)

### Unassigned Function Codes

`0x0, 0x1, 0x3, 0x5, 0x7, 0x9, 0xB, 0xD, 0xE, 0xF`

### Hypothesized Discrete Codes (Untested)

Based on the binary pattern structure:

| Guess          | Code (bin) | Code (hex) | Reasoning                                          |
|----------------|-----------|-----------|----------------------------------------------------|
| Fan Off        | `0001`    | 0x1       | LSB only - possible discrete off                   |
| Reverse/Dir    | `0011`    | 0x3       | Between Power (0010) and Oscillate (0100)           |
| Speed 1 / Low  | `0101`    | 0x5       | Between Oscillate (0100) and Speed (0110)           |
| Speed 2 / Med  | `0111`    | 0x7       | Between Speed (0110) and Timer (1100)               |
| Speed 3 / High | `1001`    | 0x9       | Between Light (1000) and ?                          |

### Testing Discrete Codes

To test a hypothesized code, generate the hex and send via Broadlink's `SendCode` command:

```python
TICK = 32.84

# Timing values (in microseconds, derived from existing codes)
SHORT_MARK_US = 427    # binary 0 mark (~13 ticks)
LONG_MARK_US = 1346    # binary 1 mark (~41 ticks)
LONG_SPACE_US = 1314   # data-carrying space (~40 ticks)
FRAME_GAP_US = 8538    # inter-repeat gap (~260 ticks)
TRAIL_GAP_US = 109456  # end-of-transmission gap (~3333 ticks)

def generate_fan_hex(func_code, repeats=4):
    """Generate Broadlink hex for a 12-bit fan code."""
    device = 0xD
    code_12bit = (device << 8) | (func_code << 4) | 0x00
    bits = [(code_12bit >> (11 - i)) & 1 for i in range(12)]

    pulses_us = []
    for r in range(repeats):
        for bit in bits:
            mark = LONG_MARK_US if bit else SHORT_MARK_US
            pulses_us.extend([mark, LONG_SPACE_US])
        if r < repeats - 1:
            pulses_us.extend([FRAME_GAP_US, LONG_SPACE_US])
    pulses_us.append(TRAIL_GAP_US)

    # Convert to Broadlink hex
    result = bytearray([0x26, 0x00, 0x00, 0x00])
    for pulse in pulses_us:
        tick_val = round(pulse / TICK)
        div, mod = divmod(tick_val, 256)
        if div > 0:
            result.append(0x00)
            result.append(div)
        result.append(mod)
    body_len = len(result) - 4
    result[2] = body_len & 0xFF
    result[3] = (body_len >> 8) & 0xFF
    return result.hex().upper()

# Generate all 16 possible codes (0-15) at 4 repeats
for func in range(16):
    hex_code = generate_fan_hex(func, repeats=4)
    print(f"0x{func:X}: {hex_code}")
```

**Pitfall**: The `00 01` bytes in Broadlink hex are NOT separators — they encode a single timing value of 256 ticks (~8.4ms). When generating codes, ensure each bit is a proper mark+space pair, and inter-frame gaps are encoded as single large values (260 ticks = `0x01 0x04`).

**Verification**: After generating a code, decode it back with `hex_to_pulses()` and classify marks (>800us = 1, <800us = 0) to confirm the bit pattern matches expectations.

Test codes were generated and saved to `~/.hermes/tmp/fan_ir_test_codes.json` during the 2026-06-15 session. The top candidates to test: 0x5 (Speed 1/Low), 0x7 (Speed 2/Medium), 0x9 (Speed 3/High), 0x3 (Direction/Reverse).

### Fan Speed Behavior

The Speed IR code cycles through speeds: 8-1-2-3-4-5-6-7-8-1-...

To set a specific speed, send Max first (-> speed 8) then N-1 Speed presses.

| Target Speed | Max + Speed Presses |
|-------------|-------------------|
| 1           | Max + 1 press     |
| 2           | Max + 2 presses   |
| 3           | Max + 3 presses   |
| 4           | Max + 4 presses   |
| 5           | Max + 5 presses   |
| 6           | Max + 6 presses   |
| 7           | Max + 7 presses   |
| 8           | Max only          |

### Timing Requirements

- **1-second delay** between Power and Max codes
- **1-second intervals** between Speed presses
- Codes arriving within ~100ms may be ignored by the fan
- Power and Max arriving simultaneously (~88ms apart) causes fan confusion
