# Changelog

## [0.25.0](https://github.com/vchatela-org/tributary/compare/v0.24.2...v0.25.0) (2026-10-29)


### Features

* **grafana:** read Fitbit's series in the Sleep, Activity and Healthspan dashboards ([325f69d](https://github.com/vchatela-org/tributary/commit/325f69dc831b5b825900c714ccfd5aec213b8dc3))
* **grafana:** read Fitbit's series in the Sleep, Activity and Healthspan dashboards ([#91](https://github.com/vchatela-org/tributary/issues/91)) ([6b75cdc](https://github.com/vchatela-org/tributary/commit/6b75cdc53412d91a3441bc931c76c9fec14c500d))


### Dependencies

* bump chainguard/python from `396cfa5` to `e86043e` ([a5d2b03](https://github.com/vchatela-org/tributary/commit/a5d2b039c42f611c39b8bab135c38225fb03704e))
* bump chainguard/python from `396cfa5` to `e86043e` ([#87](https://github.com/vchatela-org/tributary/issues/87)) ([950444e](https://github.com/vchatela-org/tributary/commit/950444ee5da45777089b1224d1e1b75ce87fda54))
* bump fastapi from 0.156.8 to 0.162.0 ([bc6e418](https://github.com/vchatela-org/tributary/commit/bc6e418316adc685f228705cb2efd73fa30be769))
* bump fastapi from 0.156.8 to 0.162.0 ([#88](https://github.com/vchatela-org/tributary/issues/88)) ([cb36f83](https://github.com/vchatela-org/tributary/commit/cb36f83c3d0677418e00a17b145c04017d253aba))
* bump sqlalchemy from 2.1.5 to 2.1.6 ([c35214f](https://github.com/vchatela-org/tributary/commit/c35214f0f83d10f094faf87f08a959fdaff7b3c3))
* bump sqlalchemy from 2.1.5 to 2.1.6 ([#89](https://github.com/vchatela-org/tributary/issues/89)) ([a14c874](https://github.com/vchatela-org/tributary/commit/a14c8744f7d6c67515a10d3eda91bc730e19b41c))

## [0.24.2](https://github.com/vchatela-org/tributary/compare/v0.24.1...v0.24.2) (2026-10-23)


### Dependencies

* bump chainguard/python from `6611f09` to `396cfa5` ([f5a3693](https://github.com/vchatela-org/tributary/commit/f5a36938eef9fc9558a2ce22059d8ab377fef3fa))
* bump chainguard/python from `6611f09` to `396cfa5` ([#81](https://github.com/vchatela-org/tributary/issues/81)) ([d4d738d](https://github.com/vchatela-org/tributary/commit/d4d738ddb22c38633fb172a21f0e5d631642533b))
* bump fastapi from 0.143.2 to 0.156.8 ([d1984a4](https://github.com/vchatela-org/tributary/commit/d1984a48eda6c87c4905fd54f96f6bd9ce790cc8))
* bump fastapi from 0.143.2 to 0.156.8 ([#82](https://github.com/vchatela-org/tributary/issues/82)) ([d02a3f2](https://github.com/vchatela-org/tributary/commit/d02a3f2d66a60ad135a4a428ed5a7afae82d19b2))
* bump pydantic-settings from 2.16.0 to 2.16.1 ([e974fcb](https://github.com/vchatela-org/tributary/commit/e974fcb431fd44cb48400af229610848cbdda5bb))
* bump pydantic-settings from 2.16.0 to 2.16.1 ([#85](https://github.com/vchatela-org/tributary/issues/85)) ([3500e5c](https://github.com/vchatela-org/tributary/commit/3500e5c3bc6422a295879b6ed19cea96826f749c))
* bump sqlalchemy from 2.1.4 to 2.1.5 ([113c6b8](https://github.com/vchatela-org/tributary/commit/113c6b846a731909ee592266cc2400c911821972))
* bump sqlalchemy from 2.1.4 to 2.1.5 ([#83](https://github.com/vchatela-org/tributary/issues/83)) ([8419f70](https://github.com/vchatela-org/tributary/commit/8419f709aac9af50216b366a7ef4acedc73b1fef))

## [0.24.1](https://github.com/vchatela-org/tributary/compare/v0.24.0...v0.24.1) (2026-10-21)


### Bug Fixes

* **docker:** take the Chainguard base with the patched CA bundle ([#78](https://github.com/vchatela-org/tributary/issues/78)) ([e85477c](https://github.com/vchatela-org/tributary/commit/e85477cc6b27124545e73c2668ceba6f4940409c))

## [0.24.0](https://github.com/vchatela-org/tributary/compare/v0.23.0...v0.24.0) (2026-10-21)


### Features

* **ui:** show how fresh fitbit-grafana's data is ([#75](https://github.com/vchatela-org/tributary/issues/75)) ([628d8d1](https://github.com/vchatela-org/tributary/commit/628d8d16e25ca2c260c60ecf1f20dff691351044))


### Bug Fixes

* **renpho:** log in with the app identity Renpho accepts ([#77](https://github.com/vchatela-org/tributary/issues/77)) ([74985e1](https://github.com/vchatela-org/tributary/commit/74985e1f819fadebeb03bbbeeb7cd0c6bc64b26a))

## [0.23.0](https://github.com/vchatela-org/tributary/compare/v0.22.0...v0.23.0) (2026-10-19)


### Features

* **github:** tell the runs that used GitHub's hosted runners ([#72](https://github.com/vchatela-org/tributary/issues/72)) ([4246ac4](https://github.com/vchatela-org/tributary/commit/4246ac401ad7dbd2a92daf25398d78c1e23b0fdb))


### Bug Fixes

* **whoop:** leave out an export answer that names no cycle, and say so ([#74](https://github.com/vchatela-org/tributary/issues/74)) ([a404e29](https://github.com/vchatela-org/tributary/commit/a404e29beb93439d01e93c7ae0cdd39f9767f474))

## [0.22.0](https://github.com/vchatela-org/tributary/compare/v0.21.0...v0.22.0) (2026-10-19)


### Features

* **github:** read the workflow runs, their durations and outcomes ([#69](https://github.com/vchatela-org/tributary/issues/69)) ([e2fc937](https://github.com/vchatela-org/tributary/commit/e2fc937fbb995d321d023b52843422ec55249778))

## [0.21.0](https://github.com/vchatela-org/tributary/compare/v0.20.0...v0.21.0) (2026-10-19)


### Features

* **ui:** link to Grafana's dashboards from the sidebar ([#67](https://github.com/vchatela-org/tributary/issues/67)) ([bf51199](https://github.com/vchatela-org/tributary/commit/bf51199aa9e18fc49ace1f9857b33480987de7e1))

## [0.20.0](https://github.com/vchatela-org/tributary/compare/v0.19.1...v0.20.0) (2026-10-19)


### Features

* **grafana:** Habits & recovery reads the night after an answer from any journal and the picked sources ([#66](https://github.com/vchatela-org/tributary/issues/66)) ([455058d](https://github.com/vchatela-org/tributary/commit/455058d062366f2c1ed0609cc7401e6dd1c5459a))


### Bug Fixes

* **whoop:** a journal answer lands on the day it is about, whatever the bedtime ([#65](https://github.com/vchatela-org/tributary/issues/65)) ([01dc463](https://github.com/vchatela-org/tributary/commit/01dc463f54d8527741c59b0ed67b937e46824539))

## [0.19.0](https://github.com/vchatela-org/tributary/compare/v0.18.0...v0.19.0) (2026-10-15)


### Features

* **grafana:** Healthspan prints each bar's scale underneath, as the app does ([#54](https://github.com/vchatela-org/tributary/issues/54)) ([618049b](https://github.com/vchatela-org/tributary/commit/618049b33bc04afcc41c3c5a1281d432ccd54294))

## [0.18.0](https://github.com/vchatela-org/tributary/compare/v0.17.0...v0.18.0) (2026-10-15)


### Features

* **grafana:** Healthspan on WHOOP's model, for any source ([#52](https://github.com/vchatela-org/tributary/issues/52)) ([c6ae3cb](https://github.com/vchatela-org/tributary/commit/c6ae3cb6482faf6ee7996f5f7f5c1ab8207863ef))

## [0.17.0](https://github.com/vchatela-org/tributary/compare/v0.16.0...v0.17.0) (2026-10-15)


### Features

* **grafana:** Healthspan's tiles turn yellow below a published guideline ([#50](https://github.com/vchatela-org/tributary/issues/50)) ([796cf78](https://github.com/vchatela-org/tributary/commit/796cf78e7a25013ea4ed50b6afde5c325794795c))

## [0.16.0](https://github.com/vchatela-org/tributary/compare/v0.15.0...v0.16.0) (2026-10-15)


### Features

* **grafana:** merge the homelab dashboards into Tributary's, and fix what they had found ([#48](https://github.com/vchatela-org/tributary/issues/48)) ([b017988](https://github.com/vchatela-org/tributary/commit/b01798869222781c547696ea6cdfbcc7cefc037b))
* **grafana:** one source picker per value, and the sleep score against the day's tasks ([#47](https://github.com/vchatela-org/tributary/issues/47)) ([c11ad3f](https://github.com/vchatela-org/tributary/commit/c11ad3f776d29fb3eb6b2f8e195686846d646a29))

## [0.15.0](https://github.com/vchatela-org/tributary/compare/v0.14.0...v0.15.0) (2026-10-15)


### Features

* **github:** a GitHub connector: who wrote the commits, hosted minutes against the included ones ([#45](https://github.com/vchatela-org/tributary/issues/45)) ([46d0f5d](https://github.com/vchatela-org/tributary/commit/46d0f5dab7ad36903e1c9f97ffa2c780540754e3))

## [0.14.0](https://github.com/vchatela-org/tributary/compare/v0.13.0...v0.14.0) (2026-10-14)


### Features

* **whoop:** carry the night's sleep and the day's strain on each journal answer ([#43](https://github.com/vchatela-org/tributary/issues/43)) ([20e0ee2](https://github.com/vchatela-org/tributary/commit/20e0ee2e212e68b8dad67b2a454136adbba64c29))

## [0.13.0](https://github.com/vchatela-org/tributary/compare/v0.12.0...v0.13.0) (2026-10-14)


### Features

* **whoop:** a sport tag on workouts, and the cycle's recovery on each journal answer ([#41](https://github.com/vchatela-org/tributary/issues/41)) ([7ba1405](https://github.com/vchatela-org/tributary/commit/7ba14054a486d9af6e89bd59665078c7dd159793))

## [0.12.0](https://github.com/vchatela-org/tributary/compare/v0.11.0...v0.12.0) (2026-10-14)


### Features

* **sleep-as-android:** a Sleep as Android backup connector ([#39](https://github.com/vchatela-org/tributary/issues/39)) ([5b402c2](https://github.com/vchatela-org/tributary/commit/5b402c217108b196971d624321c424a537e152f6))

## [0.11.0](https://github.com/vchatela-org/tributary/compare/v0.10.1...v0.11.0) (2026-10-14)


### Features

* **grafana:** Healthspan, Sleep, Activity & fitness and Correlations dashboards, one series and one colour per source ([#33](https://github.com/vchatela-org/tributary/issues/33)) ([0453f02](https://github.com/vchatela-org/tributary/commit/0453f02e5b0c6c6d7cb136ae4d3676cb77e185b1))
* **ui:** show how fresh garmin-grafana's data is ([#32](https://github.com/vchatela-org/tributary/issues/32)) ([38a2f20](https://github.com/vchatela-org/tributary/commit/38a2f209cd645dff9e3e8e8d7622216e2202e0fb))
* **whoop:** sleep, recovery, cycles, workouts and journal from WHOOP, built on kryoseu's Whoops ([#35](https://github.com/vchatela-org/tributary/issues/35)) ([1ff75a1](https://github.com/vchatela-org/tributary/commit/1ff75a1e6e34eed9e2769c276f6299a9cca13aa1))

## [0.10.1](https://github.com/vchatela-org/tributary/compare/v0.10.0...v0.10.1) (2026-10-14)


### Bug Fixes

* clear the CodeQL findings -- pinned actions, a least-privilege Harbor workflow, the back-redirect guard ([#27](https://github.com/vchatela-org/tributary/issues/27)) ([7d4f9f8](https://github.com/vchatela-org/tributary/commit/7d4f9f8bade948741c3bf9dabca5710b78679ce7))
* **web:** build the schedule editor's fallback redirect from the registered connector ([#30](https://github.com/vchatela-org/tributary/issues/30)) ([4762eee](https://github.com/vchatela-org/tributary/commit/4762eee0fda65debb72432f11ed1e96b60402c5d))

## [0.10.0](https://github.com/vchatela-org/tributary/compare/v0.9.0...v0.10.0) (2026-10-13)


### Features

* **grafana:** body composition, medication, nutrition & exercise and Todoist dashboards ([#25](https://github.com/vchatela-org/tributary/issues/25)) ([c406f21](https://github.com/vchatela-org/tributary/commit/c406f21317216c31f417ce01faa6b8d7f20c16ef))

## [0.9.0](https://github.com/vchatela-org/tributary/compare/v0.8.1...v0.9.0) (2026-10-13)


### Features

* **image:** a minimal non-root base, Chainguard Python 3.15 with no shell or package manager ([#23](https://github.com/vchatela-org/tributary/issues/23)) ([965846c](https://github.com/vchatela-org/tributary/commit/965846c55bcf59128fee01cfd91286c2f3798604))

## [0.8.1](https://github.com/vchatela-org/tributary/compare/v0.8.0...v0.8.1) (2026-10-13)


### Bug Fixes

* **ui:** show a compatible tool's databases as code, not with backticks ([#21](https://github.com/vchatela-org/tributary/issues/21)) ([c664797](https://github.com/vchatela-org/tributary/commit/c664797c3afd7bb9ac9022ef9c753b985664e150))

## [0.8.0](https://github.com/vchatela-org/tributary/compare/v0.7.0...v0.8.0) (2026-10-13)


### Features

* **ui:** a friendlier overview -- folded rows, a hue per category, a sidebar ([#20](https://github.com/vchatela-org/tributary/issues/20)) ([1f33360](https://github.com/vchatela-org/tributary/commit/1f3336041df61861800107f43ba3020211f1e216))


### Bug Fixes

* **medisafe:** say what the file holds when it isn't a doses export ([#17](https://github.com/vchatela-org/tributary/issues/17)) ([eccecbb](https://github.com/vchatela-org/tributary/commit/eccecbb5da2f3fe03dc19cef85a843945977d7e6))

## [0.7.0](https://github.com/vchatela-org/tributary/compare/v0.6.0...v0.7.0) (2026-10-13)


### Features

* **ui:** per-source schedules and a redesigned overview by category ([#16](https://github.com/vchatela-org/tributary/issues/16)) ([5ee6d3c](https://github.com/vchatela-org/tributary/commit/5ee6d3c05d40f6599cd342281f533b0224740b85))


### Bug Fixes

* **medisafe:** import the doses the app reports as deleted ([#14](https://github.com/vchatela-org/tributary/issues/14)) ([0f6ac80](https://github.com/vchatela-org/tributary/commit/0f6ac80ac74a774e4839e33a0ebad076c134cfa3))
