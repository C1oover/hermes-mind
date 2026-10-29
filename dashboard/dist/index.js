(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const { React } = SDK;
  const h = React.createElement;

  const QUICK_PRESETS = [
    { id: "solar_peak", label: "☀️ Solar Surplus" },
    { id: "gpu_crunch", label: "🔥 GPU + Waterloop Crunch" },
    { id: "lcm_compaction", label: "🌀 LCM Compaction" },
    { id: "praise_success", label: "✨ Warm Praise" },
    { id: "flirt_intimate", label: "🔥 Flirt Spark" },
    { id: "intimate_afterglow", label: "💞 Intimate Afterglow" },
    { id: "battery_evening_low", label: "🔋 Evening Cuddle Wind-Down" },
    { id: "conflict_critique", label: "⚡ Friction / Critique" }
  ];

  function MindHermesDashboardTab() {
    const [status, setStatus] = React.useState(null);
    const [viewMode, setViewMode] = React.useState("split");

    const fetchStatus = React.useCallback(async () => {
      try {
        const data = await SDK.fetchJSON("/api/plugins/mind-hermes/status");
        setStatus(data);
      } catch (e) {
        console.error("[mind-hermes] status error:", e);
      }
    }, []);

    React.useEffect(() => {
      fetchStatus();
      const id = setInterval(fetchStatus, 5000);
      return () => clearInterval(id);
    }, [fetchStatus]);

    const firePreset = async (presetId) => {
      try {
        const data = await SDK.fetchJSON("/api/plugins/mind-hermes/event", {
          method: "POST",
          body: JSON.stringify({ kind: "preset", target: presetId, value: 1.0 })
        });
        setStatus(data);
      } catch (e) {
        console.error(e);
      }
    };

    const forceTick = async () => {
      try {
        const data = await SDK.fetchJSON("/api/plugins/mind-hermes/tick", { method: "POST" });
        setStatus(data);
      } catch (e) {
        console.error(e);
      }
    };

    return h(
      "div",
      {
        style: {
          display: "flex",
          flexDirection: "column",
          height: "calc(100vh - 56px)",
          background: "#0d1017",
          color: "#e2e8f0",
          overflow: "hidden"
        }
      },
      h(
        "div",
        {
          style: {
            padding: "8px 14px",
            background: "#141821",
            borderBottom: "1px solid #252d3d",
            display: "flex",
            flexDirection: "column",
            gap: "6px",
            flexShrink: 0
          }
        },
        h(
          "div",
          { style: { display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "8px" } },
          h(
            "div",
            { style: { display: "flex", alignItems: "center", gap: "10px" } },
            h("span", { style: { fontWeight: 700, color: "#60a5fa", fontSize: "13px" } }, "🧠 Live Hermes Mind State"),
            status &&
              h(
                "span",
                {
                  style: {
                    fontSize: "11px",
                    fontFamily: "ui-monospace, monospace",
                    background: "#1e2636",
                    padding: "2px 8px",
                    borderRadius: "4px",
                    color: "#93c5fd"
                  }
                },
                "Temp Hint: " + status.sampling_temperature_hint + " · Wake(Out=" + (status.wake?.fire_outreach ?? 0).toFixed(2) + ", Silent=" + (status.wake?.fire_background ?? 0).toFixed(2) + ")"
              )
          ),
          h(
            "div",
            { style: { display: "flex", gap: "6px", flexWrap: "wrap" } },
            QUICK_PRESETS.map((p) =>
              h(
                "button",
                {
                  key: p.id,
                  onClick: () => firePreset(p.id),
                  style: {
                    background: "#1c2230",
                    color: "#cbd5e1",
                    border: "1px solid #2e384d",
                    borderRadius: "4px",
                    padding: "3px 8px",
                    fontSize: "11px",
                    cursor: "pointer"
                  }
                },
                p.label
              )
            ),
            h(
              "button",
              {
                onClick: forceTick,
                style: {
                  background: "#1e3a8a",
                  color: "#fff",
                  border: "1px solid #3b82f6",
                  borderRadius: "4px",
                  padding: "3px 9px",
                  fontSize: "11px",
                  cursor: "pointer"
                }
              },
              "+1m Live Tick"
            ),
            h(
              "button",
              {
                onClick: () => setViewMode(viewMode === "split" ? "workbench" : "split"),
                style: {
                  background: "#0f172a",
                  color: "#94a3b8",
                  border: "1px solid #334155",
                  borderRadius: "4px",
                  padding: "3px 8px",
                  fontSize: "11px",
                  cursor: "pointer"
                }
              },
              viewMode === "split" ? "⤢ Maximize Workbench" : "⤡ Show Live HUD"
            )
          )
        ),
        viewMode === "split" &&
          status &&
          h(
            "div",
            {
              style: {
                fontFamily: "ui-monospace, Menlo, monospace",
                fontSize: "11px",
                color: "#34d399",
                background: "#0b0e14",
                border: "1px solid #222938",
                borderRadius: "4px",
                padding: "6px 9px",
                maxHeight: "68px",
                overflowY: "auto",
                whiteSpace: "pre-wrap"
              }
            },
            status.ambient_mind + "\n" + status.state_telemetry
          )
      ),
      h("iframe", {
        src: "/api/plugins/mind-hermes/workbench",
        title: "Mind-Hermes Interactive Workbench",
        style: {
          flex: 1,
          width: "100%",
          border: "none",
          background: "#0d1017"
        }
      })
    );
  }

  window.__HERMES_PLUGINS__.register("mind-hermes", MindHermesDashboardTab);
})();

