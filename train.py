import torch
import torch.nn as nn
from torch.optim import Adam, lr_scheduler
from torch.utils.data.dataloader import DataLoader
import config
import time
import torch.distributed as dist
import os
if config.DDP:
    os.environ["CUDA_VISIBLE_DEVICES"] = "0,1"
    if torch.cuda.device_count() > 1:
        print("Let's use", torch.cuda.device_count(), "GPUs!")
    dist.init_process_group(backend="nccl")


from tqdm import tqdm
import numpy as np
import math
import utils
from config import logger
import data
import models
from torch.utils.data.distributed import DistributedSampler


class Train(object):

    def __init__(self):

        # train dataset
        logger.info('-' * 100)
        logger.info('Loading training and validation dataset')
        self.dataset = data.CodePtrDataset(mode='train')
        self.dataset_size = len(self.dataset)  # 里面含有asts、codes、nls、sources列表，每个列表有432674个元素
        logger.info('Size of training dataset: {}'.format(self.dataset_size))  # dataset_size=432674
        if config.DDP:
            train_sampler = DistributedSampler(self.dataset)
            self.dataloader = DataLoader(dataset=self.dataset,  # 开始训练模型时，调用这个的时候执行这个
                                         batch_size=config.batch_size,
                                         shuffle=True,  # 下一批次获取顺序是否打乱
                                         # 用于分批加载使用，合并一个样本列表以形成一个小型的tensor批次
                                         collate_fn=lambda *args: utils.collate_fn(args,
                                                                                   source_vocab=self.source_vocab,
                                                                                   code_vocab=self.code_vocab,
                                                                                   ast_vocab=self.ast_vocab,
                                                                                   nl_vocab=self.nl_vocab),
                                         sampler=train_sampler)
        else:
            self.dataloader = DataLoader(dataset=self.dataset,  # 开始训练模型时，调用这个的时候执行这个
                                         batch_size=config.batch_size,
                                         shuffle=True,  # 下一批次获取顺序是否打乱
                                         # 用于分批加载使用，合并一个样本列表以形成一个小型的tensor批次
                                         collate_fn=lambda *args: utils.collate_fn(args,
                                                                                   source_vocab=self.source_vocab,
                                                                                   code_vocab=self.code_vocab,
                                                                                   ast_vocab=self.ast_vocab,
                                                                                   nl_vocab=self.nl_vocab))

        # valid dataset
        self.valid_dataset = data.CodePtrDataset(mode='valid')
        self.valid_dataset_size = len(self.valid_dataset)
        self.valid_dataloader = DataLoader(dataset=self.valid_dataset,
                                           batch_size=config.valid_batch_size,
                                           collate_fn=lambda *args: utils.collate_fn(args,
                                                                                     source_vocab=self.source_vocab,
                                                                                     code_vocab=self.code_vocab,
                                                                                     ast_vocab=self.ast_vocab,
                                                                                     nl_vocab=self.nl_vocab))
        logger.info('Size of validation dataset: {}'.format(self.valid_dataset_size))  # valid_dataset_size=19437
        logger.info('The dataset are successfully loaded')

        # vocab    ----------------？？？为什么要创建四个单独的词库
        logger.info('-' * 100)
        logger.info('Building vocabularies')

        sources, codes, asts_vocabs, nls, ast_relation = self.dataset.get_dataset()

        # 建立一个符合大小为30000的词库
        self.source_vocab = utils.build_word_vocab(dataset=sources,
                                                   vocab_name='source',
                                                   ignore_case=True,
                                                   max_vocab_size=config.source_vocab_size,
                                                   save_dir=config.vocab_root)
        self.source_vocab_size = len(self.source_vocab)
        # 476654 -> 30000
        logger.info('Size of source vocab: {} -> {}'.format(self.source_vocab.origin_size, self.source_vocab_size))

        self.code_vocab = utils.build_word_vocab(dataset=codes,
                                                 vocab_name='code',
                                                 ignore_case=True,
                                                 max_vocab_size=config.code_vocab_size,
                                                 save_dir=config.vocab_root)
        self.code_vocab_size = len(self.code_vocab)
        # 43480 -> 30000
        logger.info('Size of code vocab: {} -> {}'.format(self.code_vocab.origin_size, self.code_vocab_size))

        self.ast_vocab = utils.build_word_vocab(dataset=asts_vocabs,
                                                vocab_name='ast',
                                                ignore_case=True,
                                                max_vocab_size=config.ast_vocab_size,
                                                save_dir=config.vocab_root)
        self.ast_vocab_size = len(self.ast_vocab)
        logger.info('Size of ast vocab: {}'.format(self.ast_vocab_size))  # 58

        self.nl_vocab = utils.build_word_vocab(dataset=nls,
                                               vocab_name='nl',
                                               ignore_case=True,
                                               max_vocab_size=config.nl_vocab_size,
                                               save_dir=config.vocab_root)
        self.nl_vocab_size = len(self.nl_vocab)
        # 53785 -> 30000
        logger.info('Size of nl vocab: {} -> {}'.format(self.nl_vocab.origin_size, self.nl_vocab_size))

        logger.info('Vocabularies are successfully built')

        # model
        logger.info('-' * 100)
        logger.info('Building the model')
        self.model = models.Model(source_vocab_size=self.source_vocab_size,
                                 code_vocab_size=self.code_vocab_size,
                                 ast_vocab_size=self.ast_vocab_size,
                                 nl_vocab_size=self.nl_vocab_size)

        # model device
        logger.info('Model device: {}'.format(next(self.model.parameters()).device))
        # log model statistic
        logger.info('Trainable parameters: {}'.format(utils.human_format(utils.count_params(self.model))))

        # optimizer
        self.optimizer = Adam([
            {'params': self.model.parameters(), 'lr': config.learning_rate},
        ])
        # 交叉熵损失函数
        self.criterion = nn.CrossEntropyLoss(ignore_index=self.nl_vocab.get_pad_index())
        # self.criterion = nn.NLLLoss(ignore_index=self.nl_vocab.get_pad_index())

        if config.use_lr_decay:
            self.lr_scheduler = lr_scheduler.StepLR(self.optimizer,
                                                    step_size=1,
                                                    gamma=config.lr_decay_rate)

        # early stopping
        self.early_stopping = None
        if config.use_early_stopping:
            self.early_stopping = utils.EarlyStopping(patience=config.early_stopping_patience,
                                                      high_record=False)
        self.start_epoch = 0
        if config.continue_train:
            self.start_epoch = 21
            # 要记得修改config里面的学习率
            path_checkpoint = "../output/Funcom/20221105_100329/models/epoch_%d.pt" % self.start_epoch
            logger.info("加载epoch_%d.pt参数成功！！！" % self.start_epoch)
            checkpoint = torch.load(path_checkpoint)  # 加载断点
            self.model.load_state_dict(checkpoint)  # 加载模型学习参数

    def run(self):

        logger.info('Start training')

        self.train_iter()

        logger.info('Training completed')

        return self.early_stopping.best_model.state_dict(), (self.source_vocab, self.code_vocab,
                                                             self.ast_vocab, self.nl_vocab)

    def train_one_batch(self, batch: utils.Batch, batch_size):
        """
        train one batch
        :param batch: get from collate_fn of corresponding dataloader, class Batch
        :param batch_size: batch size
        :return: avg loss
        """
        nl_batch = batch.extend_nl_batch if config.use_pointer_gen else batch.nl_batch

        self.optimizer.zero_grad()

        decoder_outputs = self.model(batch, batch_size, self.nl_vocab)  # [T, B, nl_vocab_size]
        # print(next(self.model.parameters()).is_cuda)
        batch_nl_vocab_size = decoder_outputs.size()[2]  # config.nl_vocab_size (+ max_oov_num)
        decoder_outputs = decoder_outputs.view(-1, batch_nl_vocab_size)
        nl_batch = nl_batch.view(-1)

        loss = self.criterion(decoder_outputs, nl_batch)
        loss.backward()

        # address over fit
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), 5)

        self.optimizer.step()

        return loss.item()

    def train_iter(self):

        loss = utils.AverageMeter()
        epoch_times = []
        # plot_train_loss = []
        # plot_train_perplexity = []
        for epoch in range(self.start_epoch+1, config.n_epochs + 1):
            epoch_time = utils.Timer()
            epoch_bar = tqdm(
                self.dataloader,
                desc='epoch: {}/{} [loss: {:.4f}, perplexity: {:.4f}]'.format(self.start_epoch+1, config.n_epochs, 0.0000, 0.0000))
            # 会调用utils.py里的collate_fn函数转化索引
            for index_batch, batch in enumerate(epoch_bar):

                batch_size = batch.batch_size

                batch_loss = self.train_one_batch(batch, batch_size)
                loss.update(batch_loss, batch_size)

                # plot_train_loss.append(loss.avg)
                # plot_train_perplexity.append(math.exp(loss.avg))

                epoch_bar.set_description('Epoch: {}/{} [loss: {:.4f}, perplexity: {:.4f}]'.format(
                    epoch, config.n_epochs, loss.avg, math.exp(loss.avg)
                ))

                if index_batch % config.log_state_every == 0:
                    logger.info('Epoch: {}/{}, time: {:.2f}, loss: {:.4f}, perplexity: {:.4f}'.format(
                        epoch, config.n_epochs, epoch_time.time(), loss.avg, math.exp(loss.avg)
                    ))
                torch.cuda.empty_cache()

            epoch_time.stop()
            epoch_times.append(epoch_time.time())

            logger.info('Epoch {} finished, time: {:.2f}, loss: {:.4f}, perplexity: {:.4f}'.format(
                epoch, epoch_time.time(), loss.avg, math.exp(loss.avg)
            ))

            loss.reset()

            self.validate(epoch)

            if config.use_early_stopping:
                if self.early_stopping.early_stop:
                    break

            if config.use_lr_decay:
                self.lr_scheduler.step()

            logger.info('learning rate: {:.6f}'.format(self.optimizer.param_groups[0]['lr']))

        logger.info('Training finished, best model at the end of epoch {}'.format(self.early_stopping.best_epoch))
        # 绘制Loss曲线图
        # utils.plot_data(plot_train_loss, "plot_train_loss")
        # utils.plot_data(plot_train_perplexity, "plot_train_perplexity")

        # save best model, i.e. model with min valid loss
        path = self.save_model(name='train.train.best.pt', state_dict=self.early_stopping.best_model)
        logger.info('Best model is saved as {}'.format(path))

        # time statics
        avg_epoch_time = np.mean(epoch_times)
        logger.info('Average time consumed by each epoch: {}'.format(utils.time2str(avg_epoch_time)))

    def validate(self, epoch):

        self.model.eval()

        loss = utils.AverageMeter()

        with torch.no_grad():
            for _, batch in enumerate(tqdm(self.valid_dataloader, desc='Validating', leave=False)):
                decoder_outputs = self.model(batch, batch.batch_size, self.nl_vocab)  # [T, B, nl_vocab_size]

                batch_nl_vocab_size = decoder_outputs.size()[2]  # config.nl_vocab_size (+ max_oov_num)
                decoder_outputs = decoder_outputs.view(-1, batch_nl_vocab_size)
                nl_batch = batch.extend_nl_batch if config.use_pointer_gen else batch.nl_batch
                nl_targets = nl_batch.view(-1)

                batch_loss = self.criterion(decoder_outputs, nl_targets)
                loss.update(batch_loss.item(), batch.batch_size)

        logger.info('Validation at epoch {} completed, avg loss: {:.4f}'.format(epoch, loss.avg))

        if config.use_early_stopping:
            self.early_stopping(score=loss.avg, model=self.model, epoch=epoch)

        #if self.early_stopping.refreshed:
            # save best model
        self.save_model(name='epoch_{}.pt'.format(epoch), state_dict=self.early_stopping.best_model)

        self.model.train()

    def save_model(self, name, state_dict):
        """
        Save current model
        :param name: if given, name the model file by given name, else by current time
        :param state_dict: if given, save the given state dict, else save current model
        :return:
        """
        if state_dict is None:
            state_dict = self.get_cur_state_dict()
        if isinstance(state_dict, nn.Module):
            state_dict = state_dict.state_dict()
        model_save_path = os.path.join(
            config.model_root,
            'model_{}.pt'.format(time.strftime('%Y%m%d_%H%M%S', time.localtime())) if name is None else name)
        torch.save(state_dict, model_save_path)
        return model_save_path

    def save_check_point(self):
        pass

    def get_cur_state_dict(self) -> dict:
        """
        get current state dict of model
        :return:
        """
        state_dict = {
            'source_encoder': self.model.source_encoder.state_dict(),
            'code_encoder': self.model.code_encoder.state_dict(),
            'ast_encoder': self.model.ast_encoder.state_dict(),
            'reduce_hidden': self.model.reduce_hidden.state_dict(),
            'decoder': self.model.decoder.state_dict(),
            'optimizer': self.optimizer.state_dict(),
        }
        return state_dict
