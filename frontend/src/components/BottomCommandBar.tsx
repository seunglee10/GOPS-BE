import { type FormEvent, useEffect } from "react";

export type BottomMenuKey = "I" | "II" | "III" | "IV" | "V" | "VI";

type BottomMenuSide = "left" | "right";

type BottomCommandBarProps = {
  activeMenu: BottomMenuKey | null;
  agentBusy: boolean;
  agentInput: string;
  isChartMode: boolean;
  onAgentInputChange: (value: string) => void;
  onAgentSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCloseMenu: () => void;
  onShowTreeMap: () => void;
  onToggleMenu: (key: BottomMenuKey) => void;
};

const leftMenuKeys: BottomMenuKey[] = ["I", "II", "III"];
const rightMenuKeys: BottomMenuKey[] = ["IV", "V", "VI"];

export function BottomCommandBar({
  activeMenu,
  agentBusy,
  agentInput,
  isChartMode,
  onAgentInputChange,
  onAgentSubmit,
  onCloseMenu,
  onShowTreeMap,
  onToggleMenu
}: BottomCommandBarProps) {
  useEffect(() => {
    if (!activeMenu) {
      return undefined;
    }

    const handleOutsidePointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      if (event.target.closest(".bottom-menu-panel, .bottom-nav-actions")) {
        return;
      }
      onCloseMenu();
    };

    document.addEventListener("pointerdown", handleOutsidePointerDown, true);
    return () => document.removeEventListener("pointerdown", handleOutsidePointerDown, true);
  }, [activeMenu, onCloseMenu]);

  return (
    <>
      {activeMenu && (
        <button
          type="button"
          className="bottom-menu-dismiss-layer"
          aria-label="Close bottom menu"
          onClick={onCloseMenu}
        />
      )}
      <nav className="workspace-bottom-nav" aria-label="Workspace command bar">
        <MenuActionGroup
          side="left"
          keys={leftMenuKeys}
          activeMenu={activeMenu}
          onShowTreeMap={onShowTreeMap}
          onCloseMenu={onCloseMenu}
          onToggleMenu={onToggleMenu}
        />
        <form className="agent-box surface-recessed" onSubmit={onAgentSubmit}>
          <input
            value={agentInput}
            onChange={(event) => onAgentInputChange(event.target.value)}
            placeholder={isChartMode ? "차트에게 물어보기" : "종목을 선택한 뒤 차트에게 물어보기"}
            aria-label="Chart agent command"
            disabled={agentBusy}
          />
          <button type="submit" disabled={agentBusy}>{agentBusy ? "..." : "Run"}</button>
        </form>
        <MenuActionGroup
          side="right"
          keys={rightMenuKeys}
          activeMenu={activeMenu}
          onShowTreeMap={onShowTreeMap}
          onCloseMenu={onCloseMenu}
          onToggleMenu={onToggleMenu}
        />
      </nav>
    </>
  );
}

function MenuActionGroup({
  side,
  keys,
  activeMenu,
  onShowTreeMap,
  onCloseMenu,
  onToggleMenu
}: {
  side: BottomMenuSide;
  keys: BottomMenuKey[];
  activeMenu: BottomMenuKey | null;
  onShowTreeMap: () => void;
  onCloseMenu: () => void;
  onToggleMenu: (key: BottomMenuKey) => void;
}) {
  const isMenuOpen = activeMenu !== null && keys.includes(activeMenu);

  return (
    <div
      className={`bottom-nav-actions ${side} ${isMenuOpen ? "is-menu-open" : ""}`}
      aria-label={`Menu actions ${side}`}
    >
      <BottomMenuPanel
        side={side}
        activeKey={activeMenu}
        onShowTreeMap={onShowTreeMap}
        onClose={onCloseMenu}
      />
      {keys.map((label) => (
        <button
          key={label}
          type="button"
          className={`workspace-nav-button surface-raised ${activeMenu === label ? "is-active" : ""}`}
          aria-label={`Menu ${label}`}
          aria-expanded={activeMenu === label}
          onClick={() => onToggleMenu(label)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function BottomMenuPanel({
  side,
  activeKey,
  onShowTreeMap,
  onClose
}: {
  side: BottomMenuSide;
  activeKey: BottomMenuKey | null;
  onShowTreeMap: () => void;
  onClose: () => void;
}) {
  const sideKeys = side === "left" ? leftMenuKeys : rightMenuKeys;
  const isOpen = activeKey !== null && sideKeys.includes(activeKey);
  const menuContent = isOpen && activeKey === "I" ? (
    <button
      type="button"
      className="bottom-menu-item surface-raised"
      onClick={() => {
        onShowTreeMap();
        onClose();
      }}
    >
      홈화면
    </button>
  ) : (
    <p className="bottom-menu-empty">{isOpen && activeKey ? `${activeKey} menu` : "Menu"}</p>
  );

  return (
    <section
      className={`bottom-menu-panel surface-floating ${side} ${isOpen ? "is-open" : ""}`}
      aria-label={`${side === "left" ? "Left" : "Right"} menu panel`}
      aria-hidden={!isOpen}
    >
      <div className="bottom-menu-list">{menuContent}</div>
    </section>
  );
}
