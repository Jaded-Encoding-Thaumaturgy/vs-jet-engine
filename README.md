# vs-jet-engine 🚀

[![Lint](https://github.com/Jaded-Encoding-Thaumaturgy/vs-engine/actions/workflows/lint.yml/badge.svg)](https://github.com/Jaded-Encoding-Thaumaturgy/vs-engine/actions/workflows/lint.yml)
[![Tests](https://github.com/Jaded-Encoding-Thaumaturgy/vs-engine/actions/workflows/test.yml/badge.svg)](https://github.com/Jaded-Encoding-Thaumaturgy/vs-engine/actions/workflows/test.yml)
[![Coverage Status](https://coveralls.io/repos/github/Jaded-Encoding-Thaumaturgy/vs-engine/badge.svg?branch=main)](https://coveralls.io/github/Jaded-Encoding-Thaumaturgy/vs-engine?branch=main)

An engine for vapoursynth previewers, renderers and script analyis tools.

## Installation

```
pip install vsjetengine
```

## Using vsengine

```python
from vsengine.policy import GlobalStore, Policy
from vsengine.vpy import load_script

with Policy(GlobalStore()) as policy, load_script("/path/to/script.vpy", policy) as script:
    outputs = script.environment.outputs
    print(outputs)

```

## Documentation

- **[Environment Policy](docs/policy.md)** - Managing VapourSynth environments with stores
- **[Event Loops](docs/loops.md)** - Integration with asyncio, Trio, and custom loops
- **[Script Execution](docs/vpy.md)** - Loading and running VapourSynth scripts

## Contributing

This project is licensed under the EUPL-1.2.
When contributing to this project you accept that your code will be using this license.
By contributing you also accept any relicencing to newer versions of the EUPL at a later point in time.
