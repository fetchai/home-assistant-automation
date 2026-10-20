# Security Policy

Security is very important for Fetch.ai and its community. This add-on can act on a home — it switches
devices, changes automations and talks to agents outside the house — so this document outlines how to
report a security issue and how we handle it.

## How to Report

Please follow the steps listed below to report your bug:

- In an email, describe the issue clearly with reference to the underlying source code and indicate whether
  the bug is **Critical** or **Non-critical**.
- Attach all relevant information that is required to reproduce the bug in a test environment.
- Include the add-on version (shown on the add-on's page in Home Assistant, and in `config.yaml`), the Home
  Assistant version and installation type, and any other relevant system information.
- Include suggested solutions and/or mitigations (if known).
- Send this email to [security@fetch.ai](mailto:security@fetch.ai) and start the subject with your
  classification **Critical** or **Non-critical** followed by `[HA Automation Agent]` and a short title of
  the bug.

The Fetch team will review your information and your classification of the bug.

For non-critical bugs, the Fetch team will create an issue or a pull request allowing you to follow the
progress on the bug fix.

For critical bugs, it is important that the Fetch team has an opportunity to release a patched version
before the exploit is acknowledged publicly. Hence, critical bugs and their fixes will be shared after the
code is patched to prevent the targeting of such exploits.

## Scope

In scope — the add-on in `fetchai-automation-agent/`, in particular anything that lets someone:

- Act on a home, or read from it, without having paired from the panel — including getting around a
  pairing code's expiry, single use or attempt limit.
- Create, change or delete an automation, or make a confirmed service call (locks, alarm, covers, valves,
  notifications, Home Assistant itself), without a household member accepting it.
- Reach the agent's REST endpoints other than through Home Assistant ingress, behind a Home Assistant login.
- Give another agent more access than the household approved, or let it act on devices the household does
  not watch.
- Send passwords, tokens, keys, network addresses or the home's coordinates out of the home past the
  outbound filter described in the README's "What leaves your home".
- Read or tamper with the add-on's stored data (pairings, the agent's seed, the behavioural record) from
  outside the add-on.

Out of scope:

- Vulnerabilities in Home Assistant, the Supervisor, uAgents, Agentverse, ASI-1 or other dependencies —
  please report them upstream. uAgents is covered by the
  [Fetch bug bounty program](https://github.com/fetchai/legal/blob/main/bug_bounty.md); this add-on is not.
- A paired household member, or an agent the household approved, using the access they were given.
- The scripts in `scripts/`, which are development tools and are not part of the add-on.

Only the latest released version of the add-on is supported; fixes are released as a new version.

## Disclosure Policy

When the security team receives a security bug report, they will assign it to a primary handler. This
person will coordinate the fix and release process, involving the following steps:

- Confirm the problem and determine the affected versions.
- Audit code to find any potential similar problems.
- Prepare a fix and release it as a new add-on version as fast as possible.

## Public Discussions

Please refrain from publicly discussing a potential security vulnerability, including in GitHub issues.

It's better to discuss privately and try to find a solution first, to limit the potential impact as much as
possible.

---

Thanks for your help!
