"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Catches a widget that throws while drawing and shows `fallback` in its place.
 * A widget is code that gets fed data from a backend; one bad payload must cost
 * that one widget its view, never the whole conversation its page.
 *
 * It has to be a class: React has no hook for an error boundary.
 */
export class WidgetBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("A Jarvis widget failed to draw:", error, info.componentStack);
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
