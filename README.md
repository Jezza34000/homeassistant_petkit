<a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/banner.png" width="700"></a>

[![GitHub Release][releases-shield]][releases] [![HACS Default](https://img.shields.io/badge/HACS-Default-blue.svg?style=for-the-badge&color=41BDF5)](https://hacs.xyz/docs/faq/custom_repositories)

### Need help ? Join us :

[![Discord][discord-shield]][discord]
[![Community Forum][forum-shield]][forum]

### Enjoying this integration?

[![Sponsor Jezza34000][github-sponsor-shield]][github-sponsor] [![Static Badge][buymeacoffee-shield]][buymeacoffee]

## 🚀 Features

- **Control** : Control your Petkit devices directly from Home Assistant.
- **Monitor** : Monitor your devices status, monitor your pet's activity, feeding, drinking, and litter usage.
- **Bluetooth relay** : Automatically check your fountain over bluetooth relay.
- **Media** : Access all your media from Petkit devices directly in Home Assistant.

#### 🎞️ Video clips :

<a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/media_video.png" width="550"/></a>

#### 🖼️ Picture gallery :

<a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/media_image.png" width="550"/></a>

> [!NOTE]
>
> The media accessible on Home Assistant are ONLY those of the current day. It is not possible to download previous days.
>
> - 🖼️ Picture feature is supported for all devices with camera, **does not** require an active subscription to Care+
> - 🎞️ Video feature is supported for all devices with camera, **REQUIRE** an active subscription to Care+
> - 📹 Real-time video stream is **not supported** yet.

## 💫 Must-have cards to elevate your Petkit dashboard

- **Schedule card for feeders**

  ➡️ Get it here : [schedule-card] \
   _Thanks to @cristianchelu for this card._

    <details>
      <summary> How to configure the schedule card ? (<i>click to get full detail</i>)</summary>
      <!---->
    <br/>
    Add this card to your HA with HACS : https://github.com/cristianchelu/dispenser-schedule-card (thanks to @cristianchelu)
    <br/>
    On config card paste this :
    
    ```yaml
    type: custom:dispenser-schedule-card
    entity: sensor.yumshare_raw_distribution_data
    editable: never
    device:
      type: custom
      max_entries: 15
      min_amount: 1
      max_amount: 20
      step_amount: 1
      status_map:
        - 0 -> pending
        - 1 -> dispensed_schedule
        - 2 -> dispensed_remote
        - 3 -> dispensed_local
        - 7 -> cancelled
        - 8 -> skipped
        - 9 -> error
      status_pattern: >-
        (?<id>[0-9]{1,2}),(?<hour>[0-9]{1,2}),(?<minute>[0-9]{1,2}),(?<amount>[0-9]{1,3}),(?<status>[0-9]{1}),?
    alternate_unit:
      unit_of_measurement: g
      conversion_factor: 10
      approximate: true
    display:
      pending:
        icon: mdi:clock-outline
        label: pending
      dispensed_schedule:
        color: var(--success-color)
        icon: mdi:clock-check-outline
        label: Dispensed (scheduled)
      dispensed_remote:
        color: var(--success-color)
        icon: mdi:cellphone-check
        label: Dispensed (remote app)
      dispensed_local:
        color: var(--success-color)
        icon: mdi:account-check
        label: Dispensed (locally)
      cancelled:
        color: var(--warning-color)
        icon: mdi:cancel
        label: Cancelled
      skipped:
        color: var(--label-badge-grey)
        icon: mdi:skip-next-circle-outline
        label: Skipped (surplus control)
      error:
        color: var(--error-color)
        icon: mdi:alert-circle
        label: Failed
    ```
    
    > Not all feeders support this feature, if you don't have the sensor `raw_distribution_data`, your feeder is not supported.
    
    </details>

<a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/feed_plan2.png" width="400"></a>

- **Petkit devices card**

  ➡️ Get it here : [petkit-device-cards] \
   _Thanks to @warmfire540 for this card._

<a href=""><img src="https://github.com/homeassistant-extras/petkit-device-cards/raw/main/assets/cards.png" width="400"></a>

## ✅ Supported Devices

| **Category**     | **Name**                  | **Device**                                                                                                                                             |
| ---------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **🍗 Feeders**   | ✅ Fresh Element          | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/feeder.png" width="40"/></a>     |
|                  | ✅ Fresh Element Mini Pro | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/feedermini.png" width="40"/></a> |
|                  | ✅ Fresh Element Infinity | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/d3.png" width="40"/></a>         |
|                  | ✅ Fresh Element Solo     | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/d4.png" width="40"/></a>         |
|                  | ✅ Fresh Element Gemini   | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/d4s.png" width="40"/></a>        |
|                  | ✅ YumShare Solo          | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/d4h.png" width="40"/></a>        |
|                  | ✅ YumShare Dual-hopper   | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/d4sh.png" width="40"/></a>       |
| **🚽 Litters**   | ✅ PuraX                  | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/t3.png" width="40"/></a>         |
|                  | ✅ PuraMax                | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/t4.1.png" width="40"/></a>       |
|                  | ✅ PuraMax 2              | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/t4.png" width="40"/></a>         |
|                  | ✅ Purobot Max Pro        | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/t5.png" width="40"/></a>         |
|                  | ✅ Purobot Ultra          | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/t6.png" width="40"/></a>         |
| **⛲ Fountains** | ✅ Eversweet Solo 2       | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/5w5.png" width="40"/></a>        |
|                  | ✅ Eversweet 3 Pro        | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/4w5.png" width="40"/></a>        |
|                  | ✅ Eversweet 3 Pro UVC    | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/6w5.png" width="40"/></a>        |
|                  | ✅ Eversweet 5 Mini       | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/2w5.png" width="40"/></a>        |
|                  | ✅ Eversweet Max          | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/ctw3.png" width="40"/></a>       |
| **🧴 Purifiers** | ✅ Air Magicube           | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/k2.png" width="40"/></a>         |
|                  | ✅ Air Smart Spray        | <a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/devices/k3.png" width="40"/></a>         |

ℹ️ All other devices not listed above are **not supported**.

🆕 New devices? Want to add support for your Petkit device? Open an issue on this repository or join us on our Discord channel.

## 📦 Installation

![Minimal HA version required](https://img.shields.io/badge/Minimal%20HA%20required-2025.1.0-blue?style=for-the-badge&color=41BDF5)

Via HACS (recommended), click here :

[![Open your Home Assistant instance and open the repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg?style=flat-square)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Jezza34000&repository=homeassistant_petkit&category=integration)

Or follow these steps:

1. Open HACS (Home Assistant Community Store)
2. Click on the three dots in the top right corner
3. Click on `Custom repositories`
4. In the Repository field, enter https://github.com/Jezza34000/homeassistant_petkit/
5. In the Category field, select `Integration`
6. Click on `Add`
7. Search for `Petkit Smart Devices` in the list of integrations
8. Install the integration
9. Restart Home Assistant
10. Go to `settings` -> `integrations` -> `add integration` -> search for `Petkit Smart Devices`
11. Follow the instructions to configure the integration

## ⤵️ Migrating from [RobertD502/home-assistant-petkit](https://github.com/RobertD502/home-assistant-petkit) integration ?

Migrating is simple and seamless! Just follow these steps:

1. Install this integration directly over the existing one.
2. Restart Home Assistant to apply the changes.

That's it! 🎉

No manual configuration or re-entry of credentials is required.
Your existing credentials (username, password, and region) will be automatically preserved. \
New entities and features will be automatically created and available.

> [!NOTE]
> Some entity names may have changed. If you encounter issues with automations or scripts, you may need to update or recreate them to match the new entity names.

## 🔧 Basic Setup

Basic setup is simple, just follow the instructions in the integration setup. \
You juste need to enter your Petkit account credentials.

> [!IMPORTANT]
>
> To use both the official Petkit app AND Home Assistant simultaneously, you need two accounts:
>
> - Use your **PRIMARY** account with the official Petkit app to retain full control over device management.
> - Use your **SECONDARY** account for Home Assistant integration.
>
> Add the secondary account to your primary account's family in the Petkit app.
>
> How to create a family and add a member:
>
> 1. Open the Petkit app and log in with your primary account.
> 2. At the top of the screen, click on Family Management, then select Create a Family and follow the prompts.
> 3. Once the family is created, click on the Add Family Member button.
> 4. Add your secondary Petkit account.
> 5. Finally, log into the Home Assistant integration using your secondary account.

## ⚙️ Advanced Configuration

**Configuration :**

- Polling interval : The interval in seconds to poll the Petkit API. (default: 60)
- Smart poll : Enable adaptative scan to reduce polling interval when device is active or an event is detected. (default: true)

<a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/config.png"/></a>

**Advanced configuration (media options) :**

- Media path : The path to store media files. (default: /media)

> [!IMPORTANT]
> It's recommended to use an external storage to store media files. As the device can generate a lot of media files, it can fill up your Home Assistant storage quickly. Specially if you have "Fetch video" option enabled.
>
> ℹ️ You can mount external storage in Home Assistant via: `Settings` > `System` > `Storage` > `Add network storage`. Once mounted, use the path `/your_storage_name` to store media files on this storage.\
> ⚠️ Important: Do not use the full path shown in the "Remote share path" field it will not work. Also, keep in mind that paths are case-sensitive.

- Media refresh interval : The interval in minutes to refresh media list. (default: 5)
- Fetch image : Enable image fetching for feeders/litter with camera. (default: true)
- Fetch video : Enable video fetching for feeders/litter with camera. (default: false)
- Event type for download : The type of event to download media. (default: Eat, Feed, Toileting)
- Delete media after (days) : The number of days to keep media files. (default: 3) Set to 0 to keep all files.

<a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/media_options.png"/></a>

**Advanced configuration (bluetooth relay options) :**

- Enable bluetooth relay : Enable bluetooth relay for fountain with bluetooth, you need a relay device. (default: true)
- Bluetooth refresh interval : The interval in minutes to scan bluetooth devices. (default: 30)

<a href=""><img src="https://raw.githubusercontent.com/Jezza34000/homeassistant_petkit/refs/heads/main/images/bt_options.png"/></a>

## 🌐 Available languages

This integration is available in the following languages:

- English
- Polish (thanks to @Chriserus)
- Spanish (thanks to @joasara)
- French
- German
- Italian
- Chinese (thanks to @pujiaxun & @LvWind)
- Ukrainian (thanks to @PolarWooolf)
- Russian (thanks to @PolarWooolf)
- Swedish (thanks to @el97)

> Some translations was generated by IA and may not be accurate, if you see any mistake, open a pull request with the correction.

## 👨‍💻 Contributions are welcome!

Developers? Want to help? Join us on our Discord channel dedicated to developers and contributors.

[![Discord][discord-shield]][discord]

[Contribution guidelines](CONTRIBUTING.md)

## 🏅 Code quality

[![GitHub Activity][commits-shield]][commits] ![Project Maintenance][maintenance-shield] [![License][license-shield]](LICENSE)

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)
[![Bugs](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=bugs)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)
[![Code Smells](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=code_smells)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=coverage)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)
[![Duplicated Lines (%)](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=duplicated_lines_density)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)
[![Vulnerabilities](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=vulnerabilities)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit)

[![Lines of Code](https://sonarcloud.io/api/project_badges/measure?project=Jezza34000_homeassistant_petkit&metric=ncloc)](https://sonarcloud.io/summary/new_code?id=Jezza34000_homeassistant_petkit) _**KISS : Keep It Simple, Stupid. Less is More**_

### Petkit API client

This repository is based on my client library for the Petkit API, which can be found here : [Jezza34000/py-petkit-api](https://github.com/Jezza34000/py-petkit-api)

### Credits (thanks to)

- @ludeeus for the [integration_blueprint](https://github.com/ludeeus/integration_blueprint) template.
- @RobertD502 for the great reverse engineering done in this repository which helped a lot [home-assistant-petkit](https://github.com/RobertD502/home-assistant-petkit)

---

[homeassistant_petkit]: https://github.com/Jezza34000/homeassistant_petkit
[commits-shield]: https://img.shields.io/github/commit-activity/y/Jezza34000/homeassistant_petkit.svg?style=flat
[commits]: https://github.com/Jezza34000/homeassistant_petkit/commits/main
[discord]: https://discord.gg/Va8DrmtweP
[discord-shield]: https://img.shields.io/discord/1318098700379361362.svg?style=for-the-badge&label=Discord&logo=discord&color=5865F2
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge&label=Home%20Assistant%20Community&logo=homeassistant&color=18bcf2
[forum]: https://community.home-assistant.io/t/petkit-integration/834431
[license-shield]: https://img.shields.io/github/license/Jezza34000/homeassistant_petkit.svg??style=flat
[maintenance-shield]: https://img.shields.io/badge/maintainer-Jezza34000-blue.svg?style=flat
[releases-shield]: https://img.shields.io/github/release/Jezza34000/homeassistant_petkit.svg?style=for-the-badge&color=41BDF5
[releases]: https://github.com/Jezza34000/homeassistant_petkit/releases
[petkit-device-cards]: https://github.com/homeassistant-extras/petkit-device-cards
[schedule-card]: https://github.com/cristianchelu/dispenser-schedule-card
[github-sponsor-shield]: https://img.shields.io/badge/sponsor-Jezza34000-blue.svg?style=for-the-badge&logo=githubsponsors&color=EA4AAA
[github-sponsor]: https://github.com/sponsors/Jezza34000
[buymeacoffee-shield]: https://img.shields.io/badge/Donate-buy_me_a_coffee-yellow.svg?style=for-the-badge&logo=buy-me-a-coffee
[buymeacoffee]: https://www.buymeacoffee.com/jezza
