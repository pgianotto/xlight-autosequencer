import React, { useRef, useState, useCallback } from 'react';
import styles from './Drop.module.css';

interface Song {
  song_id: string;
  title: string;
  status: string;
  duration_ms: number;
  folder_id: string;
  imported_at: string;
  source_paths: string[];
  video_path?: string | null;
}

interface DropProps {
  /**
   * Called after a successful import response (either /api/v1/import or
   * /api/v1/import-video).
   *
   * `created` is the server's "this is a new library entry" flag. When
   * `created: false` the user dropped a file that's already in the library
   * (deduplicated by SHA-256 of the audio) and the returned song is the
   * existing record. Callers can use this to decide whether to force a
   * re-analysis rather than just showing the cached result.
   */
  onSongImported: (song: Song, created: boolean) => void;
}

const ALLOWED_AUDIO_EXTS = new Set(['.mp3', '.wav', '.flac', '.aiff', '.aif']);
const ALLOWED_VIDEO_EXTS = new Set(['.mp4', '.mov', '.avi', '.mkv', '.webm']);

function getExt(filename: string): string {
  const i = filename.lastIndexOf('.');
  return i >= 0 ? filename.slice(i).toLowerCase() : '';
}

type ImportMode = 'audio' | 'video';

export function Drop({ onSongImported }: DropProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<ImportMode>('audio');

  async function handleFile(file: File) {
    const ext = getExt(file.name);
    const allowedExts = mode === 'audio' ? ALLOWED_AUDIO_EXTS : ALLOWED_VIDEO_EXTS;
    if (!allowedExts.has(ext)) {
      setError(`Unsupported file type: ${ext}. Supported: ${[...allowedExts].join(', ')}`);
      return;
    }

    setError(null);
    setLoading(true);

    try {
      const formData = new FormData();
      const endpoint = mode === 'audio' ? '/api/v1/import' : '/api/v1/import-video';
      formData.append(mode === 'audio' ? 'audio' : 'video', file);

      const res = await fetch(endpoint, {
        method: 'POST',
        body: formData,
      });

      const body = await res.json();
      if (!res.ok) {
        setError(body?.error?.message ?? 'Import failed');
        return;
      }

      // `created` is truthy only when this is a fresh library entry.
      // Dropping a file that's already imported returns created: false.
      onSongImported(body.song, Boolean(body?.created));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Import failed');
    } finally {
      setLoading(false);
    }
  }

  function handleChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) handleFile(file);
  }

  const handleDrop = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  const handleDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
  }, []);

  const acceptAttr = mode === 'audio'
    ? [...ALLOWED_AUDIO_EXTS].join(',')
    : [...ALLOWED_VIDEO_EXTS].join(',');

  return (
    <div className={styles.root}>
      <div className={styles.modeToggle} role="tablist">
        <button
          type="button"
          data-testid="mode-audio"
          className={mode === 'audio' ? styles.modeButtonActive : styles.modeButton}
          onClick={() => setMode('audio')}
        >
          Audio file
        </button>
        <button
          type="button"
          data-testid="mode-video"
          className={mode === 'video' ? styles.modeButtonActive : styles.modeButton}
          onClick={() => setMode('video')}
        >
          Video file
        </button>
      </div>

      <div
        data-testid="drop-target"
        className={styles.dropZone}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onClick={() => inputRef.current?.click()}
      >
        <input
          data-testid="file-input"
          ref={inputRef}
          type="file"
          accept={acceptAttr}
          style={{ display: 'none' }}
          onChange={handleChange}
        />
        {loading ? (
          <p>Importing…</p>
        ) : mode === 'audio' ? (
          <>
            <p className={styles.hint}>Drop an MP3 here or click to browse</p>
            <p className={styles.sub}>Supports MP3, WAV, FLAC, AIFF</p>
          </>
        ) : (
          <>
            <p className={styles.hint}>Drop an MP4 here or click to browse</p>
            <p className={styles.sub}>
              Supports MP4, MOV, AVI, MKV, WEBM. Audio drives the sequence;
              the video can be placed on a matrix with the Video effect.
            </p>
          </>
        )}
      </div>

      {error && (
        <p data-testid="error-message" className={styles.error}>{error}</p>
      )}
    </div>
  );
}
