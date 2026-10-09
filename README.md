# Home Assistant apps

| App | What |
|---|---|
| [GMC Relay](gmc_relay/) | Receive a GQ GMC Geiger counter's upload, hand it to Home Assistant, forward it to gmcmap.com. Status panel in the sidebar. |

## Installing

Settings > Apps > App Store > ⋮ > Repositories, add `https://github.com/heidrickla/ha-gmc-relay`, then install GMC Relay. Or copy `gmc_relay/` into the host's local apps directory (`/addons`) and reload the store.

## Developing

```bash
python -m pip install ruff pytest yamllint shellcheck-py
ruff check . && ruff format --check . && python -m pytest
```

The app runs on the base image's Python 3.14; test under the same version.

## Licence

MIT. See `LICENSE`.
