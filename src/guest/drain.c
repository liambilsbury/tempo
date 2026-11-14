#include <stdio.h>
#include <termios.h>
#include <unistd.h>

int main(void) {
    if (tcdrain(STDOUT_FILENO) != 0) {
        perror("drain: tcdrain");
        return 1;
    }
    return 0;
}
