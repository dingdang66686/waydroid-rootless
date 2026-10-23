/*
 * wd-broker.c - hand a binder device fd from inside the container to the host.
 *
 * The rootless container creates its own binderfs (see devstage.c).  Its nodes
 * live in the container's mount namespace, and binderfs nodes cannot be
 * reopened through a raw mknod() of the same major:minor (ENXIO), so the host
 * cannot simply open them.  This helper runs *inside* the container, opens the
 * node, listens on a unix socket that is bind-mounted from the host, and sends
 * the open fd to whoever connects (SCM_RIGHTS).  The receiver turns it into
 * "/dev/fd/<n>" and hands it to gbinder; the kernel keeps the same binder_proc
 * / binder context alive, so the host talks to Android's servicemanager.
 *
 * It serves exactly one client per device and exits.
 *
 * Usage: wd-broker <device> <socket>
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <device> <socket>\n", argv[0]);
        return 2;
    }

    int fd = open(argv[1], O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        perror("open device");
        return 1;
    }

    int s = socket(AF_UNIX, SOCK_STREAM, 0);
    if (s < 0) {
        perror("socket");
        return 1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, argv[2], sizeof(addr.sun_path) - 1);
    unlink(argv[2]);

    if (bind(s, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        return 1;
    }
    chmod(argv[2], 0777);

    if (listen(s, 1) < 0) {
        perror("listen");
        return 1;
    }

    int c = accept(s, NULL, NULL);
    if (c < 0) {
        perror("accept");
        return 1;
    }

    char byte = 'x';
    struct iovec iov = { .iov_base = &byte, .iov_len = 1 };
    char cmsgbuf[CMSG_SPACE(sizeof(int))];
    struct msghdr msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = cmsgbuf;
    msg.msg_controllen = sizeof(cmsgbuf);

    struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
    cmsg->cmsg_level = SOL_SOCKET;
    cmsg->cmsg_type = SCM_RIGHTS;
    cmsg->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(cmsg), &fd, sizeof(int));

    if (sendmsg(c, &msg, 0) < 0) {
        perror("sendmsg");
        return 1;
    }

    /* The receiver now owns a reference; our copy can go. */
    close(fd);
    close(c);
    close(s);
    unlink(argv[2]);
    return 0;
}
