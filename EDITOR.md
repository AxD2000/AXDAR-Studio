# AXDAR episode editor

Open **Video Editor**, then select an episode in the top menu. The Media bin lists completed Takes for approved shots. AXDAR prefers accepted Takes and checks the local video against its recorded checksum when available.

1. Select **Assemble first cut** to place the approved shots in Scene and Shot order. Missing approved shots block final approval. For a short review of available media, choose **Assemble available footage**. Exports of that selection are labelled review cuts.
2. Choose a clip in the Timeline. Set its source in and out points, split it, move it, or set a fade through black. Use the Media bin to replace or reject a Take. **Flag for regeneration** keeps the shot marked in the continuity list until you clear it after review.
3. In **Draft framing, audio and subtitles**, set the preview dimensions, frame rate, padding or crop position. Padding keeps the whole image. Preview a creative crop in the Program monitor. Set the subtitle font, size, placement and color here if you want subtitles burned into the video.
4. Generate the draft. Use the Program monitor and the continuity list to review it. In **Subtitle cues**, correct start and end times in source seconds. Cues use the original dialogue from Story & Scenes or Shot Lab. Signal analysis estimates timing, and every cue can be retimed manually.
5. Approve the generated draft. Export the MP4 and download the accompanying SRT. You can change the padded output dimensions and frame rate at export. Preview and approve a new draft for changes to composition, audio or subtitle appearance.

The archive recovery control restores completed Takes to their original episode and shot IDs. If an episode still exists, recovery appends the missing shots in recovered scenes. The editor keeps all edit decisions and exported videos under `episodes/editor/`. A trim, split, replacement, reordering, subtitle edit or output-affecting setting invalidates approval. Changes to the approved shot structure, source media or Take disposition can also invalidate approval.

AXDAR trims only short boundary spans detected as both silent and static on shots without dialogue or protected sound requirements. It keeps uncertain spans for review. FFmpeg conforms picture and audio, stabilizes the timebase, and decodes the complete result to verify it. Visual continuity, exact dialogue timing and creative framing still need human review.

Install a recent build of FFmpeg with ffprobe, H.264, AAC and libass. AXDAR checks the folder set by `AXDAR_FFMPEG_BIN`, then `tools/ffmpeg/bin/` inside Studio, then PATH. The Windows setup can use a portable build. Run `python -B -m unittest discover -q` from the repository to verify. The media-specific checks run when FFmpeg is available.
