/* CPU reference APP. The five positions are deliberately distinguishable. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 7 || strcmp(argv[1], "algorithm") != 0)
        return 2;
    unsigned int words[5], value = 0;
    for (int i = 0; i < 5; ++i) {
        char *end;
        const char *arg = argv[i + 2];
        long word = strtol(arg, &end, 10);
        if (!*arg || *end || word < 0 || word > 255)
            return 2;
        words[i] = (unsigned int)word;
        value += (i + 1) * words[i];
    }
    /* Give the bench telemetry poller a window to observe this short task. */
    sleep(1);
    printf("{\"words\":[%u,%u,%u,%u,%u],\"value\":%u}\n",
           words[0], words[1], words[2], words[3], words[4], value);
    return 0;
}
