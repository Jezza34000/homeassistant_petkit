# Security Policy

The `homeassistant_petkit` project team takes security seriously. This document describes the actively maintained versions and the responsible disclosure procedure to follow if you discover a security vulnerability.

## Supported Versions

We strongly encourage you to always use the latest version of the integration to benefit from bug fixes and security patches.

_Note: Security fixes are applied only to the main branch (`main`) and released in the current version via HACS._

## Reporting a Vulnerability

⚠️ **Important: Please do NOT open a public GitHub Issue to report a security vulnerability.**

If you discover a vulnerability (e.g., a flaw allowing access to PetKit credentials, a camera stream leak, or unwanted code execution within Home Assistant), please contact me privately via Discord.

### How to reach me?

1. Join the community Discord server (the link is available in the repository's `README.md`).
2. Contact me directly via Direct Message (DM) **@jezza8837**

### What your report should include

To help me reproduce and fix the issue as quickly as possible, please include the following details in your message:

- The version of `homeassistant_petkit` you are using.
- Your Home Assistant version (`Core`, `OS`, or `Container`).
- The hardware device involved (e.g., PuraMax, Purobot, etc.).
- A detailed description of the vulnerability.
- Steps to reproduce the issue.
- The potential impact if the flaw is exploited (e.g., PetKit API token leak).
- Any relevant log files.

Thank you for helping keep this integration secure!
