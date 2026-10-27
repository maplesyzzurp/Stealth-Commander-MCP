import com.sun.tools.attach.VirtualMachine;
import java.nio.file.Path;
import java.util.Locale;

public final class HarnessAttach {
    private HarnessAttach() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 4) {
            throw new IllegalArgumentException(
                    "Usage: HarnessAttach PID EXPECTED_CONFIG_PATH AGENT_JAR COMMAND_PROPERTIES");
        }

        Path expectedPath = Path.of(args[1]).toAbsolutePath().normalize();
        VirtualMachine vm = VirtualMachine.attach(args[0]);
        try {
            String liveConfig = vm.getSystemProperties().getProperty("idea.config.path");
            if (liveConfig == null || liveConfig.isBlank()) {
                throw new IllegalStateException("The target JVM has no idea.config.path property");
            }
            Path livePath = Path.of(liveConfig).toAbsolutePath().normalize();
            boolean windows = System.getProperty("os.name", "").toLowerCase(Locale.ROOT).contains("win");
            boolean matches = windows
                    ? livePath.toString().equalsIgnoreCase(expectedPath.toString())
                    : livePath.equals(expectedPath);
            if (!matches) {
                throw new IllegalStateException("Unexpected IDE profile: " + livePath
                        + " (expected " + expectedPath + ")");
            }

            vm.loadAgent(Path.of(args[2]).toAbsolutePath().normalize().toString(), args[3]);
        } finally {
            vm.detach();
        }
    }
}
