"""
Test script demonstrating waveform postprocessing features

This script tests:
- Simple average postprocessing
- Moving average postprocessing
- Displaying original waveforms alongside processed data
"""

import logging
import time

import numpy as np
from waveform_postprocessor import WaveformPostprocessor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def generate_test_waveform(num_samples=1000, noise_level=10):
    """Generate a test waveform with signal + noise"""
    time_axis = np.linspace(0, 1, num_samples)
    signal = 100 * np.sin(2 * np.pi * 5 * time_axis)  # 5 Hz sine wave
    noise = noise_level * np.random.randn(num_samples)
    return signal + noise


def test_simple_average():
    """Test simple average postprocessing"""
    logger.info("\n=== Testing Simple Average ===")

    # Configure postprocessor
    config = {
        "show_original": True,
        "channels": {"A": {"type": "simple_average", "window_size": 5}},
    }

    postprocessor = WaveformPostprocessor(config)

    # Simulate 10 waveform captures
    publish_count = 0
    for i in range(10):
        waveform = generate_test_waveform(noise_level=20)
        # Reshape to (1, num_samples) to match expected format
        channels_mv = {"A": waveform.reshape(1, -1)}

        processed, original, info = postprocessor.process_waveforms(channels_mv)

        if processed is not None:
            publish_count += 1
            logger.info(
                f"Capture {i+1}: ✓ Averaged data ready! Window size = {info['A']['window_size']} - Publishing #{publish_count}"
            )
            logger.info(f"  Processed shape: {processed['A'].shape}")
            logger.info(
                f"  Original shape: {original['A'].shape if original else 'None'}"
            )
        else:
            logger.info(
                f"Capture {i+1}: Collecting samples... NOT publishing (need {5 - ((i+1) % 5 if (i+1) % 5 != 0 else 5)} more)"
            )

    logger.info(
        f"\nSimple average test completed: Published {publish_count} times (expected 2)"
    )
    assert publish_count == 2, f"Expected 2 publishes, got {publish_count}"
    logger.info("✓ Simple average publishes only every N waveforms as expected\n")


def test_moving_average():
    """Test moving average postprocessing"""
    logger.info("\n=== Testing Moving Average ===")

    # Configure postprocessor
    config = {
        "show_original": False,
        "channels": {"A": {"type": "moving_average", "window_size": 3}},
    }

    postprocessor = WaveformPostprocessor(config)

    # Simulate 8 waveform captures
    for i in range(8):
        waveform = generate_test_waveform(noise_level=15)
        # Reshape to (1, num_samples) to match expected format
        channels_mv = {"A": waveform.reshape(1, -1)}

        processed, original, info = postprocessor.process_waveforms(channels_mv)

        if processed is not None:
            logger.info(
                f"Capture {i+1}: Moving average ready! "
                f"Current samples = {info['A']['current_samples']}"
            )
        else:
            logger.info(f"Capture {i+1}: Collecting samples (need 3)...")

    logger.info("Moving average test completed\n")


def test_mixed_postprocessing():
    """Test multiple channels with different postprocessing"""
    logger.info("\n=== Testing Mixed Postprocessing ===")

    # Configure postprocessor with different settings per channel
    config = {
        "show_original": True,
        "channels": {
            "A": {"type": "none", "window_size": 10},
            "B": {"type": "simple_average", "window_size": 3},
            "C": {"type": "moving_average", "window_size": 2},
        },
    }

    postprocessor = WaveformPostprocessor(config)

    # Simulate 5 waveform captures
    for i in range(5):
        waveforms = {
            "A": generate_test_waveform(noise_level=10).reshape(1, -1),
            "B": generate_test_waveform(noise_level=20).reshape(1, -1),
            "C": generate_test_waveform(noise_level=15).reshape(1, -1),
        }

        processed, original, info = postprocessor.process_waveforms(waveforms)

        logger.info(f"\nCapture {i+1}:")
        if processed:
            for ch, data in processed.items():
                ch_info = info.get(ch, {})
                logger.info(
                    f"  Channel {ch}: {ch_info.get('type', 'unknown')} - "
                    f"Ready: {ch_info.get('ready', False)}"
                )
        else:
            logger.info("  No processed channels ready")

    logger.info("Mixed postprocessing test completed\n")


def test_config_update():
    """Test updating postprocessor configuration"""
    logger.info("\n=== Testing Configuration Update ===")

    # Initial config
    config = {
        "show_original": False,
        "channels": {"A": {"type": "simple_average", "window_size": 3}},
    }

    postprocessor = WaveformPostprocessor(config)

    # Add some data
    for i in range(2):
        waveform = generate_test_waveform().reshape(1, -1)
        channels_mv = {"A": waveform}
        postprocessor.process_waveforms(channels_mv)
        logger.info(f"Added waveform {i+1} to buffer")

    # Update configuration (should clear buffers)
    new_config = {
        "show_original": True,
        "channels": {"A": {"type": "moving_average", "window_size": 2}},
    }
    postprocessor.update_config(new_config)
    logger.info("Configuration updated - buffers cleared")

    # Process with new config
    for i in range(3):
        waveform = generate_test_waveform().reshape(1, -1)
        channels_mv = {"A": waveform}
        processed, _, info = postprocessor.process_waveforms(channels_mv)

        if processed is not None:
            logger.info(
                f"Capture {i+1}: New config working! Type = {info['A']['type']}"
            )
        else:
            logger.info(f"Capture {i+1}: Collecting samples with new config...")

    logger.info("Configuration update test completed\n")


if __name__ == "__main__":
    logger.info("Starting postprocessing tests...")

    test_simple_average()
    time.sleep(0.5)

    test_moving_average()
    time.sleep(0.5)

    test_mixed_postprocessing()
    time.sleep(0.5)

    test_config_update()

    logger.info("\nAll postprocessing tests completed successfully!")
