#!/usr/bin/env python3
"""Per-device voices remain host-owned and do not poison device convergence."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from satellite_gateway.config_plane import ConfigPlane, ConfigValidationError


def main():
    with tempfile.TemporaryDirectory() as directory:
        plane = ConfigPlane(Path(directory) / 'state.json')
        plane.ensure_device('satellite-a')
        plane.ensure_device('satellite-b')
        record = plane.set_desired('satellite-a', {'voice': 'en_US-hfc_female-medium'})
        assert plane.voice_for('satellite-a') == 'en_US-hfc_female-medium'
        assert plane.voice_for('satellite-b') is None
        wire = plane.device_settings(record)
        assert 'voice' not in wire
        result = plane.report('satellite-a', generation=record['generation'], actual=wire,
                              health='healthy', error=None)
        assert result['converged'], result
        assert "voice" not in result["reported"]
        assert result["gateway_reported"]["voice"] == "en_US-hfc_female-medium"
        try:
            plane.set_desired('satellite-a', {'voice': '../../not-a-model'})
            raise AssertionError('path traversal accepted')
        except ConfigValidationError:
            pass
    print('PASS per-device voice ownership, isolation, convergence and validation')


if __name__ == '__main__':
    main()
