from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


class CompositionRenderError(RuntimeError):
    pass


class FFmpegCompositionRenderer:
    def __init__(self, ffmpeg_binary: str = "ffmpeg", timeout_seconds: int = 7200) -> None:
        self.ffmpeg_binary = ffmpeg_binary
        self.timeout_seconds = timeout_seconds

    async def render(
        self,
        manifest: dict,
        *,
        output_path: Path,
        resolution: str,
        aspect_ratio: str,
        fps: int,
    ) -> None:
        if shutil.which(self.ffmpeg_binary) is None:
            raise CompositionRenderError("渲染节点未安装 FFmpeg，请修复节点后重试")
        shots = list(manifest.get("shots") or [])
        if not shots:
            raise CompositionRenderError("合成清单中没有镜头")

        width, height = output_size(resolution, aspect_ratio)
        total_duration = float(manifest.get("duration_seconds") or 0)
        command = [self.ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-y"]
        for shot in shots:
            command.extend(["-i", str(shot["video_storage_path"])])

        audio_inputs: list[tuple[int, float, float, bool]] = []
        input_index = len(shots)
        for shot in shots:
            for clip in shot.get("dialogue_clips") or []:
                command.extend(["-i", str(clip["audio_storage_path"])])
                audio_inputs.append(
                    (
                        input_index,
                        float(clip.get("timeline_start_seconds") or 0),
                        float(manifest.get("dialogue_volume") or 1),
                        False,
                    )
                )
                input_index += 1
        for track_name, volume_key in (
            ("background_music", "background_music_volume"),
            ("environment_audio", "environment_volume"),
        ):
            tracks = manifest.get(track_name) or []
            if isinstance(tracks, dict):
                tracks = [tracks]
            for track in tracks:
                command.extend(["-stream_loop", "-1", "-i", str(track["storage_path"])])
                audio_inputs.append((input_index, 0, float(manifest.get(volume_key) or 0), True))
                input_index += 1

        filters: list[str] = []
        video_labels: list[str] = []
        for index, shot in enumerate(shots):
            duration = float(shot["duration_seconds"])
            label = f"v{index}"
            filters.append(
                f"[{index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={fps}[{label}]"
            )
            video_labels.append(f"[{label}]")
        filters.append(f"{''.join(video_labels)}concat=n={len(video_labels)}:v=1:a=0[vout]")

        audio_labels: list[str] = []
        fade = min(float(manifest.get("fade_seconds") or 0), total_duration / 2)
        for number, (index, delay, volume, looping) in enumerate(audio_inputs):
            label = f"a{number}"
            delay_ms = max(0, round(delay * 1000))
            chain = f"[{index}:a]aresample=48000"
            if delay_ms:
                chain += f",adelay={delay_ms}:all=1"
            chain += f",volume={volume:.3f},apad,atrim=duration={total_duration:.3f}"
            if looping and fade > 0:
                chain += (
                    f",afade=t=in:st=0:d={fade:.3f},"
                    f"afade=t=out:st={max(0, total_duration - fade):.3f}:d={fade:.3f}"
                )
            filters.append(f"{chain}[{label}]")
            audio_labels.append(f"[{label}]")
        if audio_labels:
            filters.append(
                f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:normalize=0:"
                f"duration=longest,atrim=duration={total_duration:.3f}[aout]"
            )
        else:
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={total_duration:.3f}[aout]"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(".rendering.mp4")
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-t",
                f"{total_duration:.3f}",
                str(temporary),
            ]
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
                raise CompositionRenderError(f"FFmpeg 渲染失败：{detail or '未知错误'}")
            temporary.replace(output_path)
        except TimeoutError as error:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise CompositionRenderError("章节渲染超时，请降低分辨率后重试") from error
        finally:
            temporary.unlink(missing_ok=True)


def output_size(resolution: str, aspect_ratio: str) -> tuple[int, int]:
    long_edge = {"720p": 1280, "1080p": 1920, "2K": 2560, "4K": 3840}.get(resolution, 1920)
    short_edge = {"720p": 720, "1080p": 1080, "2K": 1440, "4K": 2160}.get(resolution, 1080)
    if aspect_ratio == "9:16":
        return short_edge, long_edge
    if aspect_ratio == "1:1":
        return short_edge, short_edge
    if aspect_ratio == "4:3":
        return round(short_edge * 4 / 3 / 2) * 2, short_edge
    if aspect_ratio == "3:4":
        return short_edge, round(short_edge * 4 / 3 / 2) * 2
    return long_edge, short_edge
