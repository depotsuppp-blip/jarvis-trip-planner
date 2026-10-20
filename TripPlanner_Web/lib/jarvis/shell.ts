import { createStore, type Store } from "./store";

/**
 * The hub's layout state - is the sidebar collapsed to its rail, is the phone
 * drawer open - kept apart from the chat so that opening a drawer never touches
 * a message and a reply never touches the layout.
 *
 * `collapsed` is the one thing remembered between visits (this browser only).
 * It is read AFTER the first paint (`hydrate`), because the server cannot know
 * it: the page always starts expanded and snaps to the saved choice, and `ready`
 * keeps the sidebar's width transition off until then, so that snap is not
 * animated.
 */

const COLLAPSED_KEY = "jarvis.sidebar.collapsed";

export interface ShellState {
  collapsed: boolean;
  drawerOpen: boolean;
  /** False until the saved choice has been applied; the sidebar only animates once it is true. */
  ready: boolean;
}

export interface ShellStore extends Store<ShellState> {
  /** Applies the saved choice, then lets the layout animate. Returns the cleanup. */
  hydrate(): () => void;
  toggleCollapsed(): void;
  /** Opens the phone drawer. `opener` is the button that asked, so focus can go back to it (a click does not always focus a button, Safari's never does). */
  openDrawer(opener?: HTMLElement | null): void;
  closeDrawer(): void;
  /** Puts focus back on whatever opened the drawer. Call after it closes. */
  restoreFocus(): void;
}

// localStorage can be missing or throw (private windows, blocked site data): the sidebar then simply starts expanded.
function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // Nothing to do: the choice just is not remembered.
  }
}

export function createShellStore(): ShellStore {
  const store = createStore<ShellState>({ collapsed: false, drawerOpen: false, ready: false });
  let drawerOpener: HTMLElement | null = null;

  return {
    ...store,
    hydrate() {
      store.setState({ collapsed: readCollapsed() });
      // Two frames: the snap is painted un-animated before transitions are switched on.
      let second = 0;
      const first = window.requestAnimationFrame(() => {
        second = window.requestAnimationFrame(() => store.setState({ ready: true }));
      });
      return () => {
        window.cancelAnimationFrame(first);
        window.cancelAnimationFrame(second);
      };
    },
    toggleCollapsed() {
      const collapsed = !store.getState().collapsed;
      store.setState({ collapsed });
      writeCollapsed(collapsed);
    },
    openDrawer(opener) {
      drawerOpener = opener ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
      store.setState({ drawerOpen: true });
    },
    closeDrawer: () => store.setState({ drawerOpen: false }),
    restoreFocus() {
      if (drawerOpener?.isConnected) drawerOpener.focus();
      drawerOpener = null;
    },
  };
}
