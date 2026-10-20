import { useSyncExternalStore } from "react";

/**
 * A tiny external store: the hub's state lives OUTSIDE React, and a component
 * subscribes to only the slice it draws.
 *
 * Why not useState in the page: one `messages` array in the page's state meant
 * every reply - and every keystroke, since the draft lived there too -
 * re-rendered the whole screen, orb included, on the same thread the orb
 * paints its animation from. Here a reply re-renders the feed and nothing
 * else; the orb mode re-renders the orb and the few things that change with
 * it; the composer keeps its own draft.
 *
 * The contract a `select` function must keep: return something that is the
 * SAME value (===) while the slice it reads has not changed - a field of the
 * state, a boolean, a string - never a freshly built object or array. Derive
 * those in the component with useMemo.
 */

export interface Store<T> {
  getState(): T;
  /** Merges a patch (or the patch a function returns). A patch that changes nothing notifies nobody. */
  setState(patch: Partial<T> | ((state: T) => Partial<T>)): void;
  subscribe(listener: () => void): () => void;
}

export function createStore<T extends object>(initial: T): Store<T> {
  let state = initial;
  const listeners = new Set<() => void>();

  return {
    getState: () => state,
    setState(patch) {
      const partial = typeof patch === "function" ? patch(state) : patch;
      const keys = Object.keys(partial) as (keyof T)[];
      if (keys.every((key) => Object.is(state[key], partial[key]))) return;
      state = { ...state, ...partial };
      for (const listener of [...listeners]) listener();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** The slice of `store` that `select` picks; re-renders the caller only when that slice changes. */
export function useStore<T, S>(store: Store<T>, select: (state: T) => S): S {
  return useSyncExternalStore(
    store.subscribe,
    () => select(store.getState()),
    // The page is prerendered on the server, where the store is still its initial state.
    () => select(store.getState())
  );
}
