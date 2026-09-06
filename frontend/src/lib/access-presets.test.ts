import { describe, expect, it } from "vitest"
import { computePreset } from "@/lib/access-presets"
import type { CapabilityAccessMode, CapabilityAccessSummary } from "@/lib/api"

function group(options: CapabilityAccessMode[], mode: CapabilityAccessMode): CapabilityAccessSummary {
  return {
    name: "fixture",
    label: "Fixture",
    mode,
    capabilities: [],
    options: options.map((value) => ({ value, label: value })),
    requires_approval: false,
  }
}

describe("computePreset", () => {
  it("picks the highest-available mode each preset prefers", () => {
    const modes = computePreset(
      {
        filesystem: group(["off", "read_only", "write_access", "full_access"], "off"),
      },
      "full_autonomy",
    )
    expect(modes.filesystem).toBe("full_access")
  })

  it("falls back down the preference list when a capability lacks the preferred mode", () => {
    // desktop_screenshot-shaped group: no write_access/full_access at all.
    const modes = computePreset(
      {
        desktop_screenshot: group(["off", "read_only"], "off"),
      },
      "full_autonomy",
    )
    expect(modes.desktop_screenshot).toBe("read_only")
  })

  it("leaves a group at its current mode when neither of the preset's preferred modes is available", () => {
    // read_only prefers ["read_only", "off"]; a group offering neither
    // falls through to whatever it is already set to, per computePreset's
    // `picked ?? group.mode`.
    const modes = computePreset(
      {
        oddball: group(["write_access", "full_access"], "write_access"),
      },
      "read_only",
    )
    expect(modes.oddball).toBe("write_access")
  })

  it("read_only never picks a write-capable mode even when one is available", () => {
    const modes = computePreset(
      {
        filesystem: group(["off", "read_only", "write_access", "full_access"], "write_access"),
      },
      "read_only",
    )
    expect(modes.filesystem).toBe("read_only")
  })

  it("computes every group independently in one call", () => {
    const modes = computePreset(
      {
        filesystem: group(["off", "read_only", "write_access"], "off"),
        terminal: group(["off", "write_access"], "off"),
      },
      "approval_required",
    )
    expect(modes).toEqual({ filesystem: "write_access", terminal: "write_access" })
  })
})
