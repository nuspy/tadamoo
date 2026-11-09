package app.basalt.basalt_mobile

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Process
import java.util.concurrent.LinkedBlockingQueue
import kotlin.math.max

/**
 * Plays the PC's voice (16-bit mono PCM) as communication audio: the usage that follows the communication device chosen with
 * AudioManager (earpiece, speaker, Bluetooth headset) and the one the phone's echo canceller takes as its reference. The level
 * applies to what is already queued, so a lowered voice is heard at once.
 */
class CommPlayer {
    private var track: AudioTrack? = null
    private var rate = 0
    private var worker: Thread? = null
    private var queue = LinkedBlockingQueue<ByteArray>()
    @Volatile private var gain = 1f

    /** Starts a reply; a track already open at the same rate keeps playing what is queued. */
    @Synchronized
    fun begin(sampleRate: Int) {
        if (track != null && rate == sampleRate) return
        release()
        val min = AudioTrack.getMinBufferSize(sampleRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        require(min > 0) { "no buffer size for $sampleRate" }
        val t = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(sampleRate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build()
            )
            .setBufferSizeInBytes(max(min * 4, sampleRate))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        check(t.state == AudioTrack.STATE_INITIALIZED) { "the audio track did not start" }
        t.setVolume(gain)
        t.play()
        val q = LinkedBlockingQueue<ByteArray>()
        track = t
        queue = q
        rate = sampleRate
        worker = Thread {
            Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
            try {
                while (true) {
                    val chunk = q.take()
                    t.write(chunk, 0, chunk.size)
                }
            } catch (e: InterruptedException) {
                // the player was released
            } catch (e: IllegalStateException) {
                // the track was released while writing
            }
        }.also { it.start() }
    }

    fun add(pcm: ByteArray) {
        queue.offer(pcm)
    }

    /** Milliseconds of audio the track has played since it was created, or -1 when there is no track. */
    @Synchronized
    fun positionMs(): Long {
        val t = track ?: return -1
        return if (rate <= 0) -1 else t.playbackHeadPosition.toLong() * 1000L / rate
    }

    @Synchronized
    fun setGain(value: Float) {
        gain = value.coerceIn(0f, 1f)
        track?.setVolume(gain)
    }

    /** Drops what has not been played and frees the track. */
    @Synchronized
    fun release() {
        worker?.interrupt()
        worker = null
        queue.clear()
        track?.let {
            try {
                it.pause()
                it.flush()
                it.stop()
            } catch (e: IllegalStateException) {
                // already stopped
            }
            it.release()
        }
        track = null
        rate = 0
    }
}
