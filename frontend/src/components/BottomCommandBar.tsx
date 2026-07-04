import { ChevronDown, ChevronUp } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

export type BottomMenuKey = "I" | "II" | "III" | "IV" | "V" | "VI";
export type ChatLogEntry = {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  pending?: boolean;
};

type BottomMenuSide = "left" | "right";

type BottomCommandBarProps = {
  activeMenu: BottomMenuKey | null;
  agentBusy: boolean;
  agentInput: string;
  chatLog: ChatLogEntry[];
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
  chatLog,
  isChartMode,
  onAgentInputChange,
  onAgentSubmit,
  onCloseMenu,
  onShowTreeMap,
  onToggleMenu
}: BottomCommandBarProps) {
  const [chatPanelOpen, setChatPanelOpen] = useState(false);
  const hasFloatingPanel = activeMenu !== null || chatPanelOpen;

  useEffect(() => {
    if (!hasFloatingPanel) {
      return undefined;
    }

    const handleOutsidePointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      if (event.target.closest(".bottom-menu-panel, .bottom-nav-actions, .bottom-chat-panel, .agent-dock")) {
        return;
      }
      if (activeMenu) {
        onCloseMenu();
      }
      if (chatPanelOpen) {
        setChatPanelOpen(false);
      }
    };

    document.addEventListener("pointerdown", handleOutsidePointerDown, true);
    return () => document.removeEventListener("pointerdown", handleOutsidePointerDown, true);
  }, [activeMenu, chatPanelOpen, hasFloatingPanel, onCloseMenu]);

  const closeFloatingPanels = () => {
    if (activeMenu) {
      onCloseMenu();
    }
    setChatPanelOpen(false);
  };

  const toggleChatPanel = () => {
    setChatPanelOpen((current) => {
      const next = !current;
      if (next && activeMenu) {
        onCloseMenu();
      }
      return next;
    });
  };

  const toggleBottomMenu = (key: BottomMenuKey) => {
    setChatPanelOpen(false);
    onToggleMenu(key);
  };

  const submitAgentPrompt = (event: FormEvent<HTMLFormElement>) => {
    if (agentInput.trim()) {
      if (activeMenu) {
        onCloseMenu();
      }
      setChatPanelOpen(true);
    }
    onAgentSubmit(event);
  };

  return (
    <>
      {hasFloatingPanel && (
        <button
          type="button"
          className="bottom-menu-dismiss-layer"
          aria-label="Close bottom floating panel"
          onClick={closeFloatingPanels}
        />
      )}
      <nav className="workspace-bottom-nav" aria-label="Workspace command bar">
        <MenuActionGroup
          side="left"
          keys={leftMenuKeys}
          activeMenu={activeMenu}
          onShowTreeMap={onShowTreeMap}
          onCloseMenu={onCloseMenu}
          onToggleMenu={toggleBottomMenu}
        />
        <div className={`agent-dock ${chatPanelOpen ? "is-chat-open" : ""}`}>
          <button
            type="button"
            className="agent-dock-toggle"
            aria-label={chatPanelOpen ? "채팅 로그 닫기" : "채팅 로그 열기"}
            aria-expanded={chatPanelOpen}
            onClick={toggleChatPanel}
          >
            {chatPanelOpen ? <ChevronDown size={18} /> : <ChevronUp size={18} />}
          </button>
          <section className={`bottom-chat-panel surface-floating ${chatPanelOpen ? "is-open" : ""}`} aria-label="Chart agent conversation" aria-hidden={!chatPanelOpen}>
            <div className="bottom-chat-log" role="log" aria-live="polite">
              {chatLog.length ? chatLog.map((entry) => (
                <article key={entry.id} className={`bottom-chat-message ${entry.role} ${entry.pending ? "is-pending" : ""}`}>
                  <span className="bottom-chat-message-role">{entry.role === "user" ? "You" : entry.role === "assistant" ? "Agent" : "System"}</span>
                  <p>{entry.text}</p>
                </article>
              )) : (
                <p className="bottom-chat-empty">질문을 입력하면 이곳에 대화가 남습니다.</p>
              )}
            </div>
          </section>
          <form className="agent-box surface-recessed" onSubmit={submitAgentPrompt}>
            <input
              value={agentInput}
              onChange={(event) => onAgentInputChange(event.target.value)}
              placeholder={isChartMode ? "차트에게 물어보기" : "종목을 선택한 뒤 차트에게 물어보기"}
              aria-label="Chart agent command"
              disabled={agentBusy}
            />
            <button type="submit" disabled={agentBusy}>{agentBusy ? "..." : "Run"}</button>
          </form>
        </div>
        <MenuActionGroup
          side="right"
          keys={rightMenuKeys}
          activeMenu={activeMenu}
          onShowTreeMap={onShowTreeMap}
          onCloseMenu={onCloseMenu}
          onToggleMenu={toggleBottomMenu}
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
