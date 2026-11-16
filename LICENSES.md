# Licenses

## Application code

The DRIFTLINE Orchestrator source code in this package is provided under the
MIT License.

```text
MIT License

Copyright (c) 2026 DRIFTLINE

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Third-party Python dependencies

| Package | License | Purpose |
|---|---|---|
| PyYAML 6.0.3 | MIT | configuration parsing |
| httpx 0.28.1 | BSD-3-Clause | HTTP client (MCP, Qwen, Ollama, OmniRoute) |
| pytest 9.2.0 | MIT | test runner (development only) |

## Not included

* No Roblox game assets, models, or DRIFTLINE source files are included.
* No THX-02 or Qwen model weights are included; they are downloaded
  separately from their official distributions.
* No Ollama or Python binaries are included; the installer downloads them from
  their official sources after signature verification.
* No credentials of any kind are included.
