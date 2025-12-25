# vs-engine

An engine for vapoursynth previewers, renderers and script analyis tools.

## Installation

```
pip install vsengine-jet
```

## Using vsengine

Look at this example:

```py
import vapoursynth as vs
from vsengine.vpy import script

script("/script/to/my.vpy").result()
vs.get_output(0).output()
```

## Contributing

This project is licensed under the EUPL-1.2.
When contributing to this project you accept that your code will be using this license.
By contributing you also accept any relicencing to newer versions of the EUPL at a later point in time.
