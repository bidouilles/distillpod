import { create } from 'zustand';

/**
 * A signal that something was kept.
 *
 * Distillations and bookmarks are made in the fullscreen player and shown on
 * the episode page and in Saved — sibling screens with no way to tell each
 * other. So the lists only refreshed when the player happened to close, and a
 * distill tapped while it stayed open appeared nowhere until a reload.
 *
 * A counter rather than the data itself: the lists already know how to fetch,
 * they just need to know when.
 */
interface SavedSignal {
  distills: number;
  bookmarks: number;
  distillSaved: () => void;
  bookmarkSaved: () => void;
}

export const useSaved = create<SavedSignal>((set) => ({
  distills: 0,
  bookmarks: 0,
  distillSaved: () => set(s => ({ distills: s.distills + 1 })),
  bookmarkSaved: () => set(s => ({ bookmarks: s.bookmarks + 1 })),
}));
