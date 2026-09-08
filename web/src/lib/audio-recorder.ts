/** Shared microphone acquisition and encoding for voice samples and quick transcription. */
export async function openMicrophone(deviceId = ''): Promise<MediaStream> {
  if (!window.isSecureContext) {
    throw new Error(
      'Microphone access requires HTTPS or localhost. You can upload a file instead.'
    );
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    throw new Error(
      'This browser does not support microphone recording. You can upload a file instead.'
    );
  }
  try {
    return await navigator.mediaDevices.getUserMedia({
      audio: deviceId ? { deviceId: { exact: deviceId } } : true
    });
  } catch (error) {
    if (!deviceId) throw error;
    return navigator.mediaDevices.getUserMedia({ audio: true });
  }
}

export function createAudioRecorder(stream: MediaStream): MediaRecorder {
  const mimeType = [
    'audio/webm;codecs=opus',
    'audio/ogg;codecs=opus',
    'audio/mp4',
    'audio/webm'
  ].find((type) => MediaRecorder.isTypeSupported(type));
  return new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
}
