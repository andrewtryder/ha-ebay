# Changelog

## [0.6.2](https://github.com/andrewtryder/ha-ebay/compare/v0.6.1...v0.6.2) (2026-07-14)


### Bug Fixes

* clear repairs when optional features are disabled ([6152c55](https://github.com/andrewtryder/ha-ebay/commit/6152c55557c918ad12783538f5b52746167ef66b))
* persist repair streaks and analytics missing-scope detection ([6595bd4](https://github.com/andrewtryder/ha-ebay/commit/6595bd4572e367b9ed7d8dfd8a38226941141ff9))
* persist repair streaks and detect analytics missing scope at runtime ([6222a76](https://github.com/andrewtryder/ha-ebay/commit/6222a761f9e1df3fbf770033ef1dd6826a483cbd))
* persist repair streaks and detect analytics missing scope at runtime ([#53](https://github.com/andrewtryder/ha-ebay/issues/53)) ([69a5166](https://github.com/andrewtryder/ha-ebay/commit/69a516665ef528bc101a5007a415abd67f6d4936))

## [0.6.1](https://github.com/andrewtryder/ha-ebay/compare/v0.6.0...v0.6.1) (2026-07-14)


### Bug Fixes

* gate repairs and seller-ops refresh correctness ([#44](https://github.com/andrewtryder/ha-ebay/issues/44)) ([8e78ac5](https://github.com/andrewtryder/ha-ebay/commit/8e78ac598b20b791cd942f4e43ba85ac75de44ca))
* map unknown eBay enums to UNKNOWN ([#45](https://github.com/andrewtryder/ha-ebay/issues/45)) ([b7c6ecb](https://github.com/andrewtryder/ha-ebay/commit/b7c6ecb5971b5acc74ac697b2bc5b6492c9f8a2a))
* require Selling when Analytics is enabled ([#46](https://github.com/andrewtryder/ha-ebay/issues/46)) ([843c4a8](https://github.com/andrewtryder/ha-ebay/commit/843c4a8c17feda3fb0b1ab8b3be282c0fb456e29))

## [0.6.0](https://github.com/andrewtryder/ha-ebay/compare/v0.5.3...v0.6.0) (2026-07-13)


### Features

* add importable notification automation blueprints ([7c44113](https://github.com/andrewtryder/ha-ebay/commit/7c441130c5dd9516179d6ff01d2ae3b17eb1540e))
* add problem binary sensors for seller-ops health ([5cb6861](https://github.com/andrewtryder/ha-ebay/commit/5cb6861e09c4b8b2e5db38f16f72d2cb6fd25bd7))
* add repairs issues and per-section refresh schedules ([3e0c265](https://github.com/andrewtryder/ha-ebay/commit/3e0c265527e55d8e92d476da30dbaebe52d551d4))
* blueprints, repairs, refresh schedules, and options sections ([d2cd4bc](https://github.com/andrewtryder/ha-ebay/commit/d2cd4bc49fbfa63e18582479185b94cf37c5c1bd))
* emit confirmed sold and unsold selling events ([7fe351c](https://github.com/andrewtryder/ha-ebay/commit/7fe351c8c659d67f0561657bc841c04f5297dd8c))
* make OAuth scopes modular and decouple seller-ops fetches ([f9d2580](https://github.com/andrewtryder/ha-ebay/commit/f9d2580267dc01eb7725e3eb3d6fb9d53b8fb89b))
* modular OAuth scopes, seller-ops semantics, and confirmed sold/unsold events ([863d611](https://github.com/andrewtryder/ha-ebay/commit/863d6111a01fbe75800a521b66b4a2d0538da25c))
* organize options flow into menu sections ([af4f633](https://github.com/andrewtryder/ha-ebay/commit/af4f633f1dc3494a04467754eec13d93bf1a3ed2))
* reduce entity clutter and improve seller-ops sensor semantics ([a0ac176](https://github.com/andrewtryder/ha-ebay/commit/a0ac176ce8a931159b8c662aefe30c6f2e51d31d))


### Bug Fixes

* nest repair issue fix-flow strings under step ([ec93aac](https://github.com/andrewtryder/ha-ebay/commit/ec93aac9e31e17f473730be29bee589aa23ed012))

## [0.5.3](https://github.com/andrewtryder/ha-ebay/compare/v0.5.2...v0.5.3) (2026-07-12)


### Bug Fixes

* use Home Assistant calendar entity descriptions ([156308d](https://github.com/andrewtryder/ha-ebay/commit/156308d9a32d0692d0f00083822afd83d0944ac9))
* use Home Assistant calendar entity descriptions ([bd92eed](https://github.com/andrewtryder/ha-ebay/commit/bd92eed6814be90e10d060cbf6c1b0dff0331f7c))

## [0.5.2](https://github.com/andrewtryder/ha-ebay/compare/v0.5.1...v0.5.2) (2026-07-12)


### Bug Fixes

* use valid feedback OAuth scope ([30f6ad0](https://github.com/andrewtryder/ha-ebay/commit/30f6ad0c665f764c78b833fddb35df7b58fe2bce))
* use valid feedback OAuth scope ([4e5187d](https://github.com/andrewtryder/ha-ebay/commit/4e5187dd078a6cbda806c5caa9c2b7c6cf496b72))

## [0.5.1](https://github.com/andrewtryder/ha-ebay/compare/v0.5.0...v0.5.1) (2026-07-12)


### Bug Fixes

* make reauth credentials schema frontend-serializable ([1c2b074](https://github.com/andrewtryder/ha-ebay/commit/1c2b074f9a2daefca009ad129ba618925d4f94d2))
* make reauth credentials schema frontend-serializable ([05c6986](https://github.com/andrewtryder/ha-ebay/commit/05c69865ab85350da1b960b1bd1d70f7903610ed))

## [0.5.0](https://github.com/andrewtryder/ha-ebay/compare/v0.4.1...v0.5.0) (2026-07-12)


### Features

* add read-only seller-ops monitoring and telemetry hardening ([69e2b98](https://github.com/andrewtryder/ha-ebay/commit/69e2b98ce77fcdf3f9a4f03fdc14a9ac78739f6d))
* add read-only seller-ops monitoring and telemetry hardening ([bd54d96](https://github.com/andrewtryder/ha-ebay/commit/bd54d96c632f4624289a5960d394ce0daaa2e84d))

## [0.4.1](https://github.com/andrewtryder/ha-ebay/compare/v0.4.0...v0.4.1) (2026-07-12)


### Bug Fixes

* Add events, calendars, refresh button, and health diagnostics ([c08598e](https://github.com/andrewtryder/ha-ebay/commit/c08598e75bd4c28dd250886b5435d9bb5d1d8888))

## [0.4.0](https://github.com/andrewtryder/ha-ebay/compare/v0.3.5...v0.4.0) (2026-07-11)


### Features

* Add debug visibility for eBay refreshes ([ac23449](https://github.com/andrewtryder/ha-ebay/commit/ac234496808bf3cde2d1fc26938eb13605c1f0fa))

## [0.3.5](https://github.com/andrewtryder/ha-ebay/compare/v0.3.4...v0.3.5) (2026-07-11)


### Bug Fixes

* harden truncated baselines, auth handling, and release CI ([87f88fe](https://github.com/andrewtryder/ha-ebay/commit/87f88fe42e1b66c8e14d31727d8a6dee97e12e41))
* harden truncated baselines, auth handling, and release CI ([d81b5c2](https://github.com/andrewtryder/ha-ebay/commit/d81b5c273819098f2068c7885285b381352d8575))

## [0.3.4](https://github.com/andrewtryder/ha-ebay/compare/v0.3.3...v0.3.4) (2026-07-11)


### Bug Fixes

* ebay telemetry edge cases ([e3c23a3](https://github.com/andrewtryder/ha-ebay/commit/e3c23a332bf6d4b46114053b05b79a0d1c0880eb))
* ending-soon stale timer validation ([c0d20c6](https://github.com/andrewtryder/ha-ebay/commit/c0d20c67b416fa38cdfc9786d201fee3b977a1d3))

## [0.3.3](https://github.com/andrewtryder/ha-ebay/compare/v0.3.2...v0.3.3) (2026-07-11)


### Bug Fixes

* eBay event semantics and optional telemetry ([d205b45](https://github.com/andrewtryder/ha-ebay/commit/d205b458b1835907fcffc6e8fa66588760ba266a))

## [0.3.2](https://github.com/andrewtryder/ha-ebay/compare/v0.3.1...v0.3.2) (2026-07-11)


### Bug Fixes

* align release-please version handling ([dcc0679](https://github.com/andrewtryder/ha-ebay/commit/dcc06793af12bef9f37954ca2ae101374e7ecdc0))
* align release-please version handling ([49ea289](https://github.com/andrewtryder/ha-ebay/commit/49ea289b5170c2952a2637cbd1cb479e9875b3a2))

## [0.3.1](https://github.com/andrewtryder/ha-ebay/compare/v0.3.0...v0.3.1) (2026-07-10)


### Bug Fixes

* classify OAuth token failures and harden ending-soon events ([a79eb3b](https://github.com/andrewtryder/ha-ebay/commit/a79eb3b76ae99dbb78732e8d99655d9abe85659b))

## [0.3.0](https://github.com/andrewtryder/ha-ebay/compare/v0.2.0...v0.3.0) (2026-07-10)


### Features

* guide eBay OAuth setup flow ([508dec2](https://github.com/andrewtryder/ha-ebay/commit/508dec2cb25d16981665493ca75515cf556c3e36))

## [0.2.0](https://github.com/andrewtryder/ha-ebay/compare/v0.1.0...v0.2.0) (2026-07-10)


### Features

* native Home Assistant OAuth for eBay ([bb5bae5](https://github.com/andrewtryder/ha-ebay/commit/bb5bae5139fb96b818a1a9b0a559a1589b437aa7))


### Bug Fixes

* HA devcontainer readiness and pin 2026.3.0 ([451e302](https://github.com/andrewtryder/ha-ebay/commit/451e302d4fab0c4a637fae5ab58c5225d915fcdf))
