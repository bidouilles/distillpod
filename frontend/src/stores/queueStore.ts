import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import {
  clearQueue, dequeueEpisode, enqueueEpisode, getQueue, replaceQueue,
  type QueueRow,
} from '../api/client';

export interface QueueItem {
  episodeId: string;
  title: string;
  podcastTitle: string;
  audioUrl: string;
  imageUrl?: string;
  durationSeconds?: number;
}

interface QueueStore {
  queue: QueueItem[];
  hydrated: boolean;
  /** Pull the server's copy, once, at startup. */
  hydrate: () => Promise<void>;
  addNext: (item: QueueItem) => void;
  addToEnd: (item: QueueItem) => void;
  remove: (episodeId: string) => void;
  reorder: (from: number, to: number) => void;
  clear: () => void;
  shift: () => QueueItem | undefined;
  playNow: (index: number) => QueueItem | undefined;
}

const fromRow = (r: QueueRow): QueueItem => ({
  episodeId: r.episode_id,
  title: r.title,
  podcastTitle: r.podcast_title ?? "",
  audioUrl: r.audio_url,
  imageUrl: r.image_url,
  durationSeconds: r.duration_seconds ?? undefined,
});

/**
 * Send a mutation, and make the server agree with us if it does not arrive.
 *
 * Every local change is applied first, so dragging a row never waits on a
 * round trip. If the call fails — offline, a lost connection mid-drag — the
 * whole order is pushed instead, which is idempotent and always resolves to
 * what this device shows. Failing that too, the local copy stands and the next
 * mutation carries it up; the alternative, rolling back the UI, would throw
 * away the user's intent to preserve a copy nobody can see.
 */
function push(call: Promise<unknown>, order: () => string[]) {
  call.catch(() => replaceQueue(order()).catch(() => {}));
}

export const useQueue = create<QueueStore>()(
  persist(
    (set, get) => ({
      queue: [],
      hydrated: false,

      hydrate: async () => {
        try {
          const rows = await getQueue();
          set({ queue: rows.map(fromRow), hydrated: true });
        } catch {
          // Offline, or a session that has expired. The local mirror is a
          // perfectly good queue to keep playing from.
          set({ hydrated: true });
        }
      },

      addNext: (item) => {
        set((s) => ({
          queue: [item, ...s.queue.filter(q => q.episodeId !== item.episodeId)],
        }));
        push(enqueueEpisode(item.episodeId, "next"), () => get().queue.map(q => q.episodeId));
      },

      addToEnd: (item) => {
        set((s) => ({
          queue: [...s.queue.filter(q => q.episodeId !== item.episodeId), item],
        }));
        push(enqueueEpisode(item.episodeId, "end"), () => get().queue.map(q => q.episodeId));
      },

      remove: (id) => {
        const present = get().queue.some(q => q.episodeId === id);
        set((s) => ({ queue: s.queue.filter(q => q.episodeId !== id) }));
        // Playing an episode directly removes it from the queue, which happens
        // for episodes that were never in it — so only tell the server about
        // removals that actually removed something.
        if (present) {
          push(dequeueEpisode(id), () => get().queue.map(q => q.episodeId));
        }
      },

      reorder: (from, to) => {
        set((s) => {
          const q = [...s.queue];
          const [moved] = q.splice(from, 1);
          q.splice(to, 0, moved);
          return { queue: q };
        });
        // A drag is a whole-list intent, so send the result rather than a move.
        push(replaceQueue(get().queue.map(q => q.episodeId)), () => get().queue.map(q => q.episodeId));
      },

      clear: () => {
        set({ queue: [] });
        push(clearQueue(), () => []);
      },

      shift: () => {
        const q = get().queue;
        if (q.length === 0) return undefined;
        const next = q[0];
        set({ queue: q.slice(1) });
        push(dequeueEpisode(next.episodeId), () => get().queue.map(x => x.episodeId));
        return next;
      },

      playNow: (index: number) => {
        const q = get().queue;
        if (index < 0 || index >= q.length) return undefined;
        const item = q[index];
        set({ queue: q.filter((_, i) => i !== index) });
        push(dequeueEpisode(item.episodeId), () => get().queue.map(x => x.episodeId));
        return item;
      },
    }),
    {
      name: 'distillpod-queue',
      // `hydrated` describes this session's conversation with the server, not
      // the queue, so persisting it would make a cold start think it had
      // already reconciled.
      partialize: (s) => ({ queue: s.queue }),
    },
  ),
);
