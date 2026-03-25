import os
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import soundfile as sf
import whisper


OUTPUT_DIR = Path("audios")
DEFAULT_MODEL = "base"


def garantir_pasta() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def gravar_microfone(duracao_segundos: int = 8, sample_rate: int = 16000) -> Path:
    garantir_pasta()
    nome = f"gravacao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
    caminho = OUTPUT_DIR / nome

    print(f"\nGravando por {duracao_segundos} segundos... fale agora.")
    audio = sd.rec(
        int(duracao_segundos * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    sf.write(caminho, audio, sample_rate)
    print(f"Audio salvo em: {caminho}")
    return caminho


def transcrever_audio(modelo_whisper: whisper.Whisper, caminho_audio: str) -> str:
    if not os.path.exists(caminho_audio):
        raise FileNotFoundError(f"Arquivo nao encontrado: {caminho_audio}")

    resultado = modelo_whisper.transcribe(caminho_audio, language="pt")
    return resultado.get("text", "").strip()


def escolher_opcao() -> str:
    print("\n=== Bot Whisper (Audio -> Texto) ===")
    print("1) Gravar audio pelo microfone")
    print("2) Usar arquivo de audio existente")
    print("0) Sair")
    return input("Escolha uma opcao: ").strip()


def main() -> None:
    print("Carregando modelo Whisper... (pode demorar na primeira vez)")
    modelo = whisper.load_model(DEFAULT_MODEL)
    print(f"Modelo carregado: {DEFAULT_MODEL}")

    while True:
        opcao = escolher_opcao()

        if opcao == "0":
            print("Encerrando. Ate mais!")
            break

        try:
            if opcao == "1":
                duracao = input("Duracao da gravacao em segundos (padrao 8): ").strip()
                duracao_segundos = int(duracao) if duracao else 8
                caminho = gravar_microfone(duracao_segundos=duracao_segundos)
                texto = transcrever_audio(modelo, str(caminho))
            elif opcao == "2":
                caminho = input("Digite o caminho do arquivo de audio: ").strip().strip('"')
                texto = transcrever_audio(modelo, caminho)
            else:
                print("Opcao invalida. Tente novamente.")
                continue

            print("\n--- TEXTO TRANSCRITO ---")
            print(texto if texto else "(Nenhum texto reconhecido)")
            print("------------------------")
        except Exception as erro:
            print(f"\nErro: {erro}")
            print(
                "Dica: verifique se o FFmpeg esta instalado e disponivel no PATH "
                "e se o arquivo de audio e valido."
            )


if __name__ == "__main__":
    main()
