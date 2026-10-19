# EmpirioLabs AI Media for Open WebUI

Create videos, images and speech in Open WebUI chats with models from the [EmpirioLabs AI](https://empiriolabs.ai) catalog. Ask for a video, an image or a voice-over in the chat, and the model calls this tool and shows the result in the conversation.

EmpirioLabs AI is a specialized AI inference and integration provider. It self-hosts open-source models on GPU infrastructure it manages, runs optimized endpoints for proprietary models, helps teams ship their own models to large audiences, and offers on-demand GPU Cloud instances and hosted AI agents. Its model catalog covers text, image, audio, video, transcription, embedding, reranking, research, and 3D models.

## What the tool adds

| Function | What it does |
| --- | --- |
| `create_video` | Creates a video from a prompt, or animates a start image (image-to-video). Options: duration, resolution, aspect ratio, sound on or off, and model. The chat shows the job's progress while the video renders, then plays the video. |
| `check_video` | Shows a video that was still rendering when the wait limit passed, using its job ID. |
| `create_image` | Creates an image from a prompt, with an optional aspect ratio and model. The image is shown in the chat and saved in Open WebUI. |
| `create_speech` | Turns text into speech with an optional voice and model, and shows an audio player in the chat. |
| `list_media_models` | Lists the video, image or speech models the tool can use, with their resolutions, aspect ratios, durations and voices. |

Model lists, defaults and voices come from the live EmpirioLabs AI model catalog (`GET https://api.empiriolabs.ai/v1/models`), so new models appear without a tool update.

To animate an image you just created, ask for it in the same chat: `create_image` returns an `image_id`, and `create_video` accepts it as the start image. A public `https://` link to an image works too.

## Requirements

- Open WebUI 0.12 or later.
- A chat model that supports tool calling. Open WebUI uses native function calling by default.
- An EmpirioLabs AI API key.

## Installation

1. In Open WebUI, open **Workspace**, then **Tools**, and select **Create**.
2. Paste the contents of [`empiriolabs_media.py`](empiriolabs_media.py) into the editor and save.
3. Turn the tool on for a chat in the chat input: open **Integrations**, then **Tools**, and select **EmpirioLabs AI Media**. You can also attach the tool to a model in that model's settings, so the model can use it in every chat.

## API key

1. Sign in or create an account at [platform.empiriolabs.ai](https://platform.empiriolabs.ai).
2. Open [API Keys](https://platform.empiriolabs.ai/dashboard/api-keys) in the dashboard and create a key. Keys start with `sk-empiriolabs-`.
3. In a chat, open **Integrations**, then **Tools**, select the **Valves** button next to **EmpirioLabs AI Media**, and paste the key into **API Key**.

Each person adds their own key, so requests use their own credits. An admin can also set a shared key in the tool's Valves under **Workspace**, then **Tools**; it is used only for people who have not added their own. The key is sent only to the EmpirioLabs AI API and is never shown in the chat. Open WebUI stores Valve values in its database; set `ENABLE_VALVE_ENCRYPTION=true` on your Open WebUI server to encrypt them at rest.

## Settings

Admin Valves (**Workspace**, then **Tools**, then **Valves**):

| Valve | Default | Description |
| --- | --- | --- |
| API Key | empty | Optional shared key for people who have not added their own. |
| Image Model | `flux-2-klein-4b` | Default image model, chosen from the live catalog. |
| Video Model | `hunyuan-video-1-5` | Default video model, chosen from the live catalog. |
| Speech Model | `tts-2-flash` | Default text-to-speech model, chosen from the live catalog. |
| Longest Video (seconds) | 10 | Longest video the tool requests. |
| Video Wait (minutes) | 20 | How long the tool waits for a video before handing back its job ID. The job keeps running, and `check_video` shows it later. |
| Keep Copies | on | Keeps a copy of each video and audio file in Open WebUI, attached to the message. |
| Largest Copy (MB) | 100 | Largest file to keep a copy of. |

User Valves (chat input, **Integrations**, **Tools**, **Valves**): **API Key**, plus optional **Image Model**, **Video Model**, **Speech Model** and **Voice** choices that replace the admin defaults for that person.

If a default model leaves the catalog, the tool uses the first featured model of the same kind. A model ID that is not in the catalog is reported as an error, never replaced silently.

## Example requests

- "Create a 5-second 720p video of a paper boat drifting across a pond at sunset."
- "Make a 16:9 image of a lighthouse on a rocky coast in watercolor, then animate it with rolling waves."
- "Read this paragraph aloud with the voice Mark: ..."
- "Which video models can I use, and which ones take a start image?"

## How results appear

- **Images** appear in the assistant message and are saved in Open WebUI, like images from Open WebUI's own image generation.
- **Videos and speech** play in the chat. The player streams from the download link the API returns and shows the date that link expires. With **Keep Copies** on, a copy is also attached to the message as a file, so it stays available after the link expires.
- **Progress.** While a video renders, the status line above the reply shows the model and the elapsed time.

## Credits and prices

Requests use the credits on the EmpirioLabs AI account that owns the key. Prices for every model are listed at [empiriolabs.ai/models](https://empiriolabs.ai/models). Video is billed by length and resolution, so the duration and resolution you ask for affect the price.

## Data handling

The tool sends prompts, start-image links and the API key only to the EmpirioLabs AI API at `https://api.empiriolabs.ai`, and downloads generated files only from the `https://` links the API returns. Copies of generated files are stored in your Open WebUI. For how EmpirioLabs AI handles data, see the [privacy policy](https://empiriolabs.ai/privacy-policy).

## Development

The tests use a mocked API and never reach the network:

```sh
pip install pytest httpx pydantic fastapi
python -m pytest
```

## Links

- Website: [empiriolabs.ai](https://empiriolabs.ai)
- Documentation: [docs.empiriolabs.ai](https://docs.empiriolabs.ai)
- Models and prices: [empiriolabs.ai/models](https://empiriolabs.ai/models)
- Support: [support@empiriolabs.ai](mailto:support@empiriolabs.ai)

## License

[MIT](LICENSE)
