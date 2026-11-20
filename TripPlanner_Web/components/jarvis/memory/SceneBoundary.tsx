"use client";

import { Component, type ReactNode } from "react";
import { glassButton } from "./solarStyles";

interface SceneBoundaryProps {
  children: ReactNode;
}

interface SceneBoundaryState {
  failed: boolean;
  /** Bumped by Try again: the scene is mounted afresh. */
  attempt: number;
}

/**
 * Keeps a fault in the 3D scene (HoloScene, Three.js) inside the scene. Without it, an exception
 * while the scene renders would take the whole Memory page down - the panel, the search, the
 * list - with it; with it, the canvas says in one sentence that it could not be drawn and offers
 * Try again, and every entry is still in the panel's list, where it can be opened and corrected.
 */
export class SceneBoundary extends Component<SceneBoundaryProps, SceneBoundaryState> {
  state: SceneBoundaryState = { failed: false, attempt: 0 };

  static getDerivedStateFromError(): Partial<SceneBoundaryState> {
    return { failed: true };
  }

  componentDidCatch(error: unknown): void {
    console.error("Memory 3D view: the scene could not be drawn; the list still works.", error);
  }

  render() {
    if (!this.state.failed) {
      return <SceneAttempt key={this.state.attempt}>{this.props.children}</SceneAttempt>;
    }
    return (
      <div className="absolute inset-0 grid place-items-center px-6 pt-24 text-center">
        <div className="max-w-sm">
          <p className="text-sm font-semibold text-[#f1f5fb]">The 3D view couldn&apos;t be drawn.</p>
          <p className="mt-1 text-sm leading-6 text-[#c9d3e3]">Every entry is still in the list, where you can open and correct it.</p>
          <button
            type="button"
            onClick={() => this.setState((state) => ({ failed: false, attempt: state.attempt + 1 }))}
            className={`mt-4 ${glassButton}`}
          >
            Try again
          </button>
        </div>
      </div>
    );
  }
}

/** The scene under a key, so Try again mounts it afresh instead of reusing the instance that failed. */
function SceneAttempt({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
