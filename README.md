# Relative Light Group

A custom Home Assistant integration that creates light groups with **relative brightness control**. This integration is built upon the codebase of the official Home Assistant `group` integration, extending its native functionality.

Unlike standard light groups, this integration maintains the relative brightness ratio between lights when adjusting the group brightness. Changes to color, effects, and other attributes are only applied to lights that are currently on — off lights are never accidentally turned on by attribute changes.

## Features

- **Relative Brightness** — When you change the group brightness, each on-light's brightness is adjusted proportionally. Brighter lights stay brighter, dimmer lights stay dimmer.
- **Only-On-Lights** — Color, effect, color temperature, and other visual attribute changes are only sent to lights that are currently on. Off lights remain off.
- **Remember on/off state** — If enabled, when the group is turned off and on again, only the lights that were previously on will turn on.
- **Remember brightness** — If enabled, the group remembers each light's individual brightness. Ratios are preserved even after hitting brightness limits. Individual brightness can only be changed directly on each light, not from the group.
- **Native Group Features** — Supports the same standard options as the native integration: selecting light entities, "All entities must be on", and "Hide members".
- **Config Flow UI** — Create and configure groups entirely through the Home Assistant UI (Settings → Helpers).
- **Multi-language** — Localized in English, Spanish, German, French, Italian, and Portuguese.

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Go to **Integrations**
3. Click the three dots menu (⋮) → **Custom repositories**
4. Add this repository URL and select **Integration** as the category
5. Search for "Relative Light Group" and install it
6. Restart Home Assistant

### Manual

1. Copy the `custom_components/relative_light_group` folder to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Helpers**
2. Click **+ Create Helper**
3. Select **Relative Light Group**
4. Enter a name, select your light entities, and configure options
5. Click **Submit**

### Options

This integration supports the same standard options as the native group integration (`Light entities`, `All entities must be on`, and `Hide members`). In addition, it provides the following custom options:

| Option | Description |
|--------|-------------|
| **Remember on/off state** | If enabled, when the group is turned off and on again, only the lights that were previously on will turn on. |
| **Remember brightness** | If enabled, the group remembers each light's individual brightness. Ratios are preserved even after hitting brightness limits. Individual brightness can only be changed directly on each light, not from the group. |

## How Relative Brightness Works

When you adjust the group brightness slider:

1. The integration calculates the brightness change as a ratio
2. Each on-light receives a proportional adjustment based on its current brightness
3. Brighter lights get a larger absolute change, dimmer lights get a smaller change
4. The relative ratios between lights are maintained

**Example:** Group has Light A at 200/255 and Light B at 100/255. If you increase the group to maximum:
- Light A (closer to max) increases to 255
- Light B increases proportionally, maintaining its relative position

## Author

**Felipe Urzúa**  
Email: [cheerpipe@gmail.com](mailto:cheerpipe@gmail.com)  
Repository: [https://github.com/Cheerpipe/relative-light-group](https://github.com/Cheerpipe/relative-light-group)

## License

MIT
