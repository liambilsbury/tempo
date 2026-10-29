// SPDX-License-Identifier: GPL-2.0
// expose the KVM steal counter in ns through /proc/tempo_steal and /dev/tempo_steal

#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <asm/io.h>
#include <asm/kvm_para.h>
#include <asm/msr.h>

#define TEMPO_MAX_CPUS 64

// steal time address of a CPU
struct tempo_cpu {
	u64 gpa;
	bool enabled;
};

static struct tempo_cpu tempo_cpus[TEMPO_MAX_CPUS];
static int tempo_ncpu;

// read a CPU's steal time address from MSR_KVM_STEAL_TIME
static int tempo_probe_cpu(int cpu)
{
	u64 val = 0;

	if (rdmsrl_safe_on_cpu(cpu, MSR_KVM_STEAL_TIME, &val))
		return -ENODEV;
	if (!(val & KVM_MSR_ENABLED))
		return -ENODEV;
	tempo_cpus[cpu].gpa = val & ~(u64)KVM_MSR_ENABLED;
	tempo_cpus[cpu].enabled = true;
	return 0;
}

// read a CPU's steal value under the version seqlock
static u64 tempo_read_steal(int cpu)
{
	struct kvm_steal_time *st;
	u32 v1, v2;
	u64 steal;

	if (!tempo_cpus[cpu].enabled)
		return 0;
	st = (struct kvm_steal_time *)phys_to_virt(tempo_cpus[cpu].gpa);
	do {
		v1 = READ_ONCE(st->version);
		virt_rmb();
		steal = READ_ONCE(st->steal);
		virt_rmb();
		v2 = READ_ONCE(st->version);
	} while ((v1 & 1) || v1 != v2);
	return steal;
}

// print a line per CPU
static int tempo_show(struct seq_file *m, void *v)
{
	int cpu;

	for (cpu = 0; cpu < tempo_ncpu; cpu++) {
		if (!tempo_cpus[cpu].enabled)
			continue;
		seq_printf(m, "%d %llu %llu %llu\n", cpu,
			   (unsigned long long)tempo_cpus[cpu].gpa,
			   (unsigned long long)(tempo_cpus[cpu].gpa & ~PAGE_MASK),
			   (unsigned long long)tempo_read_steal(cpu));
	}
	return 0;
}

// open /proc/tempo_steal
static int tempo_open(struct inode *inode, struct file *file)
{
	return single_open(file, tempo_show, NULL);
}

static const struct proc_ops tempo_proc_ops = {
	.proc_open = tempo_open,
	.proc_read = seq_read,
	.proc_lseek = seq_lseek,
	.proc_release = single_release,
};

// map a CPU's steal page read-only
static int tempo_mmap(struct file *file, struct vm_area_struct *vma)
{
	unsigned long cpu = vma->vm_pgoff;
	unsigned long size = vma->vm_end - vma->vm_start;

	if (cpu >= TEMPO_MAX_CPUS || !tempo_cpus[cpu].enabled)
		return -ENODEV;
	if (size != PAGE_SIZE)
		return -EINVAL;
	if (vma->vm_flags & VM_WRITE)
		return -EPERM;
	vm_flags_clear(vma, VM_MAYWRITE);
	vma->vm_page_prot = pgprot_noncached(vma->vm_page_prot);
	vma->vm_page_prot = PAGE_READONLY;
	return remap_pfn_range(vma, vma->vm_start,
			       tempo_cpus[cpu].gpa >> PAGE_SHIFT,
			       PAGE_SIZE, vma->vm_page_prot);
}

static const struct file_operations tempo_fops = {
	.owner = THIS_MODULE,
	.mmap = tempo_mmap,
	.llseek = no_llseek,
};

static struct miscdevice tempo_misc = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "tempo_steal",
	.fops = &tempo_fops,
	.mode = 0444,
};

// probe every CPU and register the device and the proc file
static int __init tempo_init(void)
{
	int cpu, found = 0, rc;

	if (!kvm_para_available()) {
		pr_err("tempo_steal: not running under KVM paravirt; nothing to expose\n");
		return -ENODEV;
	}
	tempo_ncpu = min_t(int, num_possible_cpus(), TEMPO_MAX_CPUS);
	for (cpu = 0; cpu < tempo_ncpu; cpu++)
		if (!tempo_probe_cpu(cpu))
			found++;
	if (!found) {
		pr_err("tempo_steal: MSR_KVM_STEAL_TIME not enabled on any cpu\n");
		return -ENODEV;
	}

	rc = misc_register(&tempo_misc);
	if (rc)
		return rc;
	if (!proc_create("tempo_steal", 0444, NULL, &tempo_proc_ops)) {
		misc_deregister(&tempo_misc);
		return -ENOMEM;
	}
	pr_info("tempo_steal: %d of %d cpus expose a nanosecond steal counter\n",
		found, tempo_ncpu);
	return 0;
}

// remove the proc file and the device
static void __exit tempo_exit(void)
{
	remove_proc_entry("tempo_steal", NULL);
	misc_deregister(&tempo_misc);
}

module_init(tempo_init);
module_exit(tempo_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("expose the guest's nanosecond KVM steal counter, which /proc/stat truncates to USER_HZ");
