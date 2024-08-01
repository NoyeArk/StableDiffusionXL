import os
import sys
from glob import glob
from typing import List, Optional, Union

from tqdm import tqdm

sys.path.append(os.path.realpath(os.path.join(os.path.dirname(__file__), "../../")))
import numpy as np
import torch
from fire import Fire

from scripts.demo.sv4d_helpers import (
    decode_latents,
    load_model,
    read_video,
    run_img2vid,
    run_img2vid_per_step,
    sample_sv3d,
    save_video,
)


def sample(
    input_path: str = "assets/test_video.mp4",  # Can either be image file or folder with image files
    output_folder: Optional[str] = "outputs/sv4d",
    num_steps: Optional[int] = 20,
    sv3d_version: str = "sv3d_u",  # sv3d_u or sv3d_p
    fps_id: int = 6,
    motion_bucket_id: int = 127,
    cond_aug: float = 1e-5,
    seed: int = 23,
    decoding_t: int = 14,  # Number of frames decoded at a time! This eats most VRAM. Reduce if necessary.
    device: str = "cuda",
    elevations_deg: Optional[Union[float, List[float]]] = 10.0,
    azimuths_deg: Optional[List[float]] = None,
    image_frame_ratio: Optional[float] = None,
    verbose: Optional[bool] = False,
    remove_bg: bool = False,
):
    """
    Simple script to generate multiple novel-view videos conditioned on a video `input_path` or multiple frames, one for each
    image file in folder `input_path`. If you run out of VRAM, try decreasing `decoding_t`.
    """
    # Set model config
    T = 5  # number of frames per sample
    V = 8  # number of views per sample
    F = 8  # vae factor to downsize image->latent
    C = 4
    H, W = 576, 576
    n_frames = 21  # number of input and output video frames
    n_views = V + 1  # number of output video views (1 input view + 8 novel views)
    n_views_sv3d = 21
    subsampled_views = np.array(
        [0, 2, 5, 7, 9, 12, 14, 16, 19]
    )  # subsample (V+1=)9 (uniform) views from 21 SV3D views

    model_config = "scripts/sampling/configs/sv4d.yaml"
    version_dict = {
        "T": T * V,
        "H": H,
        "W": W,
        "C": C,
        "f": F,
        "options": {
            "discretization": 1,
            "cfg": 2.5,
            "sigma_min": 0.002,
            "sigma_max": 700.0,
            "rho": 7.0,
            "guider": 5,
            "num_steps": num_steps,
            "force_uc_zero_embeddings": [
                "cond_frames",
                "cond_frames_without_noise",
                "cond_view",
                "cond_motion",
            ],
            "additional_guider_kwargs": {
                "additional_cond_keys": ["cond_view", "cond_motion"]
            },
        },
    }

    torch.manual_seed(seed)
    os.makedirs(output_folder, exist_ok=True)

    # Read input video frames i.e. images at view 0
    print(f"Reading {input_path}")
    images_v0 = read_video(
        input_path,
        n_frames=n_frames,
        W=W,
        H=H,
        remove_bg=remove_bg,
        image_frame_ratio=image_frame_ratio,
        device=device,
    )

    # Get camera viewpoints
    if isinstance(elevations_deg, float) or isinstance(elevations_deg, int):
        elevations_deg = [elevations_deg] * n_views_sv3d
    assert (
        len(elevations_deg) == n_views_sv3d
    ), f"Please provide 1 value, or a list of {n_views_sv3d} values for elevations_deg! Given {len(elevations_deg)}"
    if azimuths_deg is None:
        azimuths_deg = np.linspace(0, 360, n_views_sv3d + 1)[1:] % 360
    assert (
        len(azimuths_deg) == n_views_sv3d
    ), f"Please provide a list of {n_views_sv3d} values for azimuths_deg! Given {len(azimuths_deg)}"
    polars_rad = np.array([np.deg2rad(90 - e) for e in elevations_deg])
    azimuths_rad = np.array(
        [np.deg2rad((a - azimuths_deg[-1]) % 360) for a in azimuths_deg]
    )

    # Sample multi-view images of the first frame using SV3D i.e. images at time 0
    images_t0 = sample_sv3d(
        images_v0[0],
        n_views_sv3d,
        num_steps,
        sv3d_version,
        fps_id,
        motion_bucket_id,
        cond_aug,
        decoding_t,
        device,
        polars_rad,
        azimuths_rad,
        verbose,
    )
    images_t0 = torch.roll(images_t0, 1, 0)  # move conditioning image to first frame

    # Initialize image matrix
    img_matrix = [[None] * n_views for _ in range(n_frames)]
    for i, v in enumerate(subsampled_views):
        img_matrix[0][i] = images_t0[v].unsqueeze(0)
    for t in range(n_frames):
        img_matrix[t][0] = images_v0[t]

    base_count = len(glob(os.path.join(output_folder, "*.mp4"))) // 10
    save_video(
        os.path.join(output_folder, f"{base_count:06d}_t000.mp4"),
        img_matrix[0],
    )
    save_video(
        os.path.join(output_folder, f"{base_count:06d}_v000.mp4"),
        [img_matrix[t][0] for t in range(n_frames)],
    )

    # Load SV4D model
    model, filter = load_model(
        model_config,
        device,
        version_dict["T"],
        num_steps,
        verbose,
    )

    # Interleaved sampling for anchor frames
    t0, v0 = 0, 0
    frame_indices = np.arange(T - 1, n_frames, T - 1)  # [4, 8, 12, 16, 20]
    view_indices = np.arange(V) + 1
    print(f"Sampling anchor frames {frame_indices}")
    image = img_matrix[t0][v0]
    cond_motion = torch.cat([img_matrix[t][v0] for t in frame_indices], 0)
    cond_view = torch.cat([img_matrix[t0][v] for v in view_indices], 0)
    polars = polars_rad[subsampled_views[1:]][None].repeat(T, 0).flatten()
    azims = azimuths_rad[subsampled_views[1:]][None].repeat(T, 0).flatten()
    azims = (azims - azimuths_rad[v0]) % (torch.pi * 2)
    samples = run_img2vid(
        version_dict, model, image, seed, polars, azims, cond_motion, cond_view
    )
    samples = samples.view(T, V, 3, H, W)
    for i, t in enumerate(frame_indices):
        for j, v in enumerate(view_indices):
            if img_matrix[t][v] is None:
                img_matrix[t][v] = samples[i, j][None] * 2 - 1

    # Dense sampling for the rest
    print(f"Sampling dense frames:")
    for t0 in tqdm(np.arange(0, n_frames - 1, T - 1)):  # [0, 4, 8, 12, 16]
        frame_indices = t0 + np.arange(T)
        print(f"Sampling dense frames {frame_indices}")
        latent_matrix = torch.randn(n_frames, n_views, C, H // F, W // F).to("cuda")
        for step in tqdm(range(num_steps)):
            frame_indices = frame_indices[
                ::-1
            ].copy()  # alternate between forward and backward conditioning
            t0 = frame_indices[0]
            image = img_matrix[t0][v0]
            cond_motion = torch.cat([img_matrix[t][v0] for t in frame_indices], 0)
            cond_view = torch.cat([img_matrix[t0][v] for v in view_indices], 0)
            polars = polars_rad[subsampled_views[1:]][None].repeat(T, 0).flatten()
            azims = azimuths_rad[subsampled_views[1:]][None].repeat(T, 0).flatten()
            azims = (azims - azimuths_rad[v0]) % (torch.pi * 2)
            noisy_latents = latent_matrix[frame_indices][:, view_indices].flatten(0, 1)
            samples = run_img2vid_per_step(
                version_dict,
                model,
                image,
                seed,
                polars,
                azims,
                cond_motion,
                cond_view,
                step,
                noisy_latents,
            )
            samples = samples.view(T, V, C, H // F, W // F)
            for i, t in enumerate(frame_indices):
                for j, v in enumerate(view_indices):
                    latent_matrix[t, v] = samples[i, j]

        for t in frame_indices:
            for v in view_indices:
                if t != 0 and v != 0:
                    img = decode_latents(model, latent_matrix[t, v][None], T)
                    img_matrix[t][v] = img * 2 - 1

    # Save output videos
    for v in view_indices:
        vid_file = os.path.join(output_folder, f"{base_count:06d}_v{v:03d}.mp4")
        print(f"Saving {vid_file}")
        save_video(vid_file, [img_matrix[t][v] for t in range(n_frames)])

    # Save diagonal video
    diag_frames = [
        img_matrix[t][(t // (n_frames // n_views)) % n_views] for t in range(n_frames)
    ]
    vid_file = os.path.join(output_folder, f"{base_count:06d}_diag.mp4")
    print(f"Saving {vid_file}")
    save_video(vid_file, diag_frames)


if __name__ == "__main__":
    Fire(sample)
