# Release verification

Keep the PC runtime, local plugin and account connector as distinct versioned components. Record their exact versions and content hashes with the gateway and authorization source commit. Never reuse a historical test report to certify modified code.

Before a source push, run PC unit tests, launcher/installer tests, gateway and authorization tests, and the release contract checks. Regenerate the complete install manifest after source changes; inspect the exact package file list and scan tracked changes for credentials, local paths and generated state.

Before promoting an installed runtime, verify an isolated install and upgrade, configuration preservation, archive integrity, rollback availability and a harmless stdio call. Test task-owned native, browser and PyCharm launch/control/observe/stop workflows serially. Inspect captures and returned cleanup results. Preserve unrelated applications and the active input desktop.

Before a hosted deployment, retain private mode, authorization-store and limiter bindings, selected-device consent, ticket verification and test billing. Run account separation, revoked/expired credentials, ticket replay, alias and tunnel lifecycle checks against the exact deployment candidate. Follow the service migration notes for legacy URL credentials.

Public submission additionally needs verified publisher identity, a public support contact, approved privacy/terms pages, supported countries, commerce decisions, dedicated reviewer access, a real demo recording, and executed positive/negative review cases. Source/runtime validation does not supply these business and legal decisions. Publisher identity is Nexusmap.ai; the sole approved brand asset is assets/ninja-mask.png.

A real clean-Windows installation and public-scale recovery/load acceptance are separate from tests run on the developer's configured machine. Record any unverified gate explicitly instead of calling it passed.
