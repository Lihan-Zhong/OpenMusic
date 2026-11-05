/**
 * DeepSeek Harness half of the OpenMusic plugin: a sidebar entry and a full page that hosts the *original*
 * workbench and practice player.  The Host half proxies the workbench under /openmusic/, same-origin, so the
 * iframe works without touching app.py.  The panel itself only adds a thin bar (folder, refresh, new tab).
 */
window.__ModuleLoader__.load({
  id: "openmusic-dsh",
  factory(require) {
    const React = require("react");
    const h = React.createElement;
    const ROOT_KEY = "openmusic-dsh.root";

    const COLORS = {
      bg: "var(--dsw-alias-bg-base)",
      layer: "var(--dsw-alias-bg-layer-1)",
      border: "var(--dsw-alias-border-l1)",
      text: "var(--dsw-alias-label-primary)",
      muted: "var(--dsw-alias-label-secondary)",
      accent: "var(--dsw-alias-brand-primary)",
      error: "var(--dsw-alias-state-error-primary)",
    };

    const buttonStyle = {
      appearance: "none",
      border: `1px solid ${COLORS.border}`,
      background: "transparent",
      color: COLORS.text,
      borderRadius: 6,
      padding: "3px 9px",
      font: "inherit",
      fontSize: 12,
      lineHeight: "18px",
      cursor: "pointer",
      whiteSpace: "nowrap",
    };

    function Icon({ size = 20 }) {
      return h(
        "svg",
        {
          width: size,
          height: size,
          viewBox: "0 0 64 64",
          fill: "none",
          "aria-hidden": "true",
          style: { display: "block" },
        },
        h("path", {
          d: "M14 34v6M22 28v18M30 20v34M38 30v14",
          stroke: "currentColor",
          strokeWidth: 5,
          strokeLinecap: "round",
        }),
        h("circle", { cx: 49, cy: 42, r: 5, fill: "currentColor" }),
        h("path", { d: "M54 18v24", stroke: "currentColor", strokeWidth: 3.5, strokeLinecap: "round" }),
      );
    }

    function Setup({ initial, error, busy, onOpen, onCancel }) {
      const [value, setValue] = React.useState(initial || "");
      return h(
        "div",
        {
          style: {
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 12,
            padding: 24,
            textAlign: "center",
          },
        },
        h("h2", { style: { margin: 0, fontSize: 16, fontWeight: 600 } }, "连接 OpenMusic"),
        h(
          "p",
          { style: { margin: 0, maxWidth: 460, color: COLORS.muted, fontSize: 13, lineHeight: 1.6 } },
          "填写 OpenMusic 项目文件夹（里面有 experiments/app.py）。插件会启动或复用工作台，原来的 Python 流程和网页都不改。",
        ),
        h("input", {
          value,
          autoFocus: true,
          spellCheck: false,
          placeholder: "/Users/you/OpenMusic",
          onChange: (event) => setValue(event.target.value),
          onKeyDown: (event) => {
            if (event.key === "Enter") onOpen(value);
          },
          style: {
            width: "min(520px, 90%)",
            boxSizing: "border-box",
            border: `1px solid ${COLORS.border}`,
            background: COLORS.bg,
            color: COLORS.text,
            borderRadius: 8,
            padding: "8px 10px",
            font: "inherit",
            fontSize: 13,
          },
        }),
        h(
          "div",
          { style: { display: "flex", gap: 8 } },
          h("button", { style: buttonStyle, disabled: busy, onClick: () => onOpen(value) }, busy ? "正在连接…" : "打开"),
          initial ? h("button", { style: buttonStyle, onClick: onCancel }, "取消") : null,
        ),
        error ? h("p", { style: { margin: 0, color: COLORS.error, fontSize: 13 } }, error) : null,
      );
    }

    function Panel() {
      const [loading, setLoading] = React.useState(true);
      const [root, setRoot] = React.useState(() => {
        try {
          return localStorage.getItem(ROOT_KEY) || "";
        } catch {
          return "";
        }
      });
      const [setup, setSetup] = React.useState(false);
      const [error, setError] = React.useState("");
      const [busy, setBusy] = React.useState(false);
      const [nonce, setNonce] = React.useState(0);

      React.useEffect(() => {
        let alive = true;
        fetch("/openmusic/info", { headers: { accept: "application/json" } })
          .then((response) => response.json())
          .then((info) => {
            if (!alive) return;
            if (info.root) {
              setRoot(info.root);
              try {
                localStorage.setItem(ROOT_KEY, info.root);
              } catch {
                /* private mode */
              }
            } else {
              setSetup(true);
              if (info.error) setError(info.error);
            }
          })
          .catch((reason) => alive && setError(String(reason)))
          .finally(() => alive && setLoading(false));
        return () => {
          alive = false;
        };
      }, []);

      const open = async (value) => {
        const path = (value || "").trim();
        if (!path) return;
        setBusy(true);
        setError("");
        try {
          const response = await fetch(`/openmusic/info?root=${encodeURIComponent(path)}`, {
            headers: { accept: "application/json" },
          });
          const info = await response.json();
          if (!info.root) {
            setError(info.error || "这个文件夹里没有 experiments/app.py");
            return;
          }
          setRoot(info.root);
          setSetup(false);
          setNonce((value) => value + 1);
          try {
            localStorage.setItem(ROOT_KEY, info.root);
          } catch {
            /* private mode */
          }
        } catch (reason) {
          setError(String(reason));
        } finally {
          setBusy(false);
        }
      };

      const url = root ? `/openmusic/app?root=${encodeURIComponent(root)}` : "";

      return h(
        "div",
        {
          style: {
            height: "100%",
            minHeight: "65vh",
            display: "flex",
            flexDirection: "column",
            background: COLORS.bg,
            color: COLORS.text,
          },
        },
        h(
          "div",
          {
            style: {
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "6px 10px",
              borderBottom: `1px solid ${COLORS.border}`,
              background: COLORS.layer,
              flex: "0 0 auto",
            },
          },
          h("span", { style: { fontWeight: 600, fontSize: 13 } }, "OpenMusic"),
          h(
            "span",
            {
              title: root || "",
              style: {
                flex: 1,
                minWidth: 0,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                fontSize: 12,
                color: COLORS.muted,
              },
            },
            root || "",
          ),
          h("button", { style: buttonStyle, title: "重新载入页面", onClick: () => setNonce((value) => value + 1) }, "刷新"),
          h(
            "button",
            {
              style: buttonStyle,
              title: "在新标签页打开",
              onClick: () => url && window.open(url, "_blank", "noopener"),
            },
            "新标签页",
          ),
          h("button", { style: buttonStyle, title: "更换 OpenMusic 项目文件夹", onClick: () => setSetup(true) }, "目录"),
        ),
        loading
          ? h(
              "div",
              { style: { flex: 1, display: "grid", placeItems: "center", color: COLORS.muted, fontSize: 13 } },
              "正在连接 OpenMusic…",
            )
          : setup || !root
            ? h(Setup, {
                initial: root,
                error,
                busy,
                onOpen: open,
                onCancel: () => {
                  if (root) setSetup(false);
                  else setError("");
                },
              })
            : h("iframe", {
                key: nonce,
                src: url,
                title: "OpenMusic",
                allow: "autoplay",
                style: { flex: 1, width: "100%", minHeight: 0, border: 0, display: "block", background: "#fff" },
              }),
      );
    }

    return {
      inject: ["slots"],
      apply(ctx) {
        ctx.slots.inject("main", () => ctx.slots.register({ name: "main", key: "openmusic" }, Panel));
        ctx.slots.inject("sidebar.panellist", () =>
          ctx.slots.register(
            { name: "sidebar.panellist", id: "openmusic", order: 20, label: () => "OpenMusic" },
            Icon,
          ),
        );
      },
    };
  },
});
