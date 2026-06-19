from setuptools import setup

APP = ['launch.py']
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'CFBundleIdentifier': 'com.personal.dictation',
        'CFBundleName': 'Personal Dictation',
        'LSUIElement': True,
        'NSMicrophoneUsageDescription': 'Personal Dictation needs microphone access to transcribe your speech.',
    },
    # Must mirror what the code actually imports. silero_vad/torch/onnxruntime
    # are a hard runtime dependency (transcribe.py runs VAD on every dictation);
    # omitting them crashes a non-alias build on first use.
    'packages': ['rumps', 'sounddevice', 'mlx_whisper', 'mlx', 'numpy',
                 'silero_vad', 'torch', 'onnxruntime'],
}

setup(
    name='Personal Dictation',
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
